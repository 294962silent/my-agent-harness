"""Opt-in bounded export. Model calls only enqueue; I/O runs on one worker.

Sinks must configure finite network timeouts. Python cannot terminate a blocked
transport thread: close(timeout) returns False if it has not drained, and may be
called again. The daemon worker then exits once the transport returns.
"""
from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
import time

from .observation import CallEnd, CallRecord


def record_dict(record: CallRecord) -> dict:
    result = {field.name: getattr(record, field.name) for field in fields(record)}
    result["kind"] = "end" if isinstance(record, CallEnd) else "start"
    if isinstance(record, CallEnd):
        result["usage"] = dict(record.usage) if record.usage is not None else None
    return result


class JsonlSink:
    """Metadata-only JSONL; wrap in BoundedExporter for model-thread isolation."""
    def __init__(self, path: str | Path):
        self.path = Path(path)

    def __call__(self, record: CallRecord) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record_dict(record), ensure_ascii=False) + "\n")


class _Flush:
    def __init__(self):
        self.done = Event()
        self.ok = False


class BoundedExporter:
    """Non-blocking observer, explicit bounded flush/close and failure counters."""
    def __init__(self, sink, *, capacity: int = 1024):
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self.sink = sink
        self._queue: Queue = Queue(maxsize=capacity)
        self._lock = Lock()
        self._closing = Event()
        self.dropped = 0
        self.export_errors = 0
        self.close_errors = 0
        self._thread = Thread(target=self._run, name="llm-observation-export", daemon=True)
        self._thread.start()

    def __call__(self, record: CallRecord) -> None:
        with self._lock:
            if self._closing.is_set():
                self.dropped += 1
                return
            try:
                self._queue.put_nowait(record)
            except Full:
                self.dropped += 1

    def _invoke(self, function, *args) -> bool:
        try:
            function(*args)
            return True
        except BaseException:
            with self._lock:
                self.export_errors += 1
            return False

    def _run(self) -> None:
        try:
            while not (self._closing.is_set() and self._queue.empty()):
                try:
                    item = self._queue.get(timeout=0.05)
                except Empty:
                    continue
                try:
                    if isinstance(item, _Flush):
                        flush = getattr(self.sink, "flush", None)
                        item.ok = self._invoke(flush) if flush is not None else True
                        item.done.set()
                    else:
                        self._invoke(self.sink, item)
                finally:
                    self._queue.task_done()
        finally:
            for name in ("flush", "close"):
                function = getattr(self.sink, name, None)
                if function is not None:
                    if not self._invoke(function):
                        with self._lock:
                            self.close_errors += 1

    def flush(self, timeout: float = 5) -> bool:
        """Wait for queued records and sink flush; never run I/O on caller thread."""
        deadline = time.monotonic() + max(0, timeout)
        marker = _Flush()
        # Serialize enqueue against close, without blocking model enqueue on I/O.
        with self._lock:
            if self._closing.is_set():
                return not self._thread.is_alive()
            try:
                self._queue.put_nowait(marker)
            except Full:
                return False
        return marker.done.wait(max(0, deadline - time.monotonic())) and marker.ok

    def close(self, timeout: float = 5) -> bool:
        """True only after worker exit and successful sink cleanup.

        Record-export failures remain visible in export_errors; close_errors
        distinguishes failed cleanup from a still-running transport thread.
        """
        with self._lock:
            self._closing.set()
        self._thread.join(max(0, timeout))
        return not self._thread.is_alive() and self.close_errors == 0

    @property
    def is_alive(self) -> bool:
        return self._thread.is_alive()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
