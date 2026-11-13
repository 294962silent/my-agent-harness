"""Trusted production retrieval worker; Linux network lockdown never reaches API stages."""

from __future__ import annotations

import argparse
import asyncio
import ctypes
import errno
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time

if __package__ in (None, ""):
    _root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_root / "src"), str(_root)]

from experiments.longmemeval.harness_corpus import ARMS, chunk_value, render_chunks


def deny_network():
    """OS filter for this dedicated process, not a generated-code sandbox.

    AF_UNIX socketpair remains available for asyncio thread notifications. New
    Internet/packet/netlink sockets, connect and destination-bearing sends are
    denied for every thread. The parent starts us without inherited sockets.
    """
    if not sys.platform.startswith("linux"):
        raise RuntimeError("retrieval CLI requires Linux libseccomp network lockdown")
    library = ctypes.CDLL("libseccomp.so.2")
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_syscall_resolve_name.restype = ctypes.c_int
    library.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.c_void_p,
    ]
    library.seccomp_attr_set.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32]
    library.seccomp_load.argtypes = [ctypes.c_void_p]
    library.seccomp_release.argtypes = [ctypes.c_void_p]

    class Comparison(ctypes.Structure):
        _fields_ = [("arg", ctypes.c_uint), ("op", ctypes.c_int), ("a", ctypes.c_uint64), ("b", ctypes.c_uint64)]

    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    if not context:
        raise RuntimeError("cannot initialize retrieval seccomp filter")
    try:
        if library.seccomp_attr_set(context, 4, 1) != 0:  # synchronize existing threads
            raise RuntimeError("cannot synchronize retrieval seccomp filter")
        for name, comparison in (
            (b"socket", Comparison(0, 1, 1, 0)),  # family != AF_UNIX
            (b"connect", None),
            (b"sendmsg", None),
            (b"sendmmsg", None),
            (b"sendto", Comparison(4, 1, 0, 0)),
        ):  # explicit destination != NULL
            number = library.seccomp_syscall_resolve_name(name)
            if (
                number < 0
                or library.seccomp_rule_add_array(
                    context,
                    0x00050000 | errno.EPERM,
                    number,
                    int(comparison is not None),
                    ctypes.byref(comparison) if comparison is not None else None,
                )
                != 0
            ):
                raise RuntimeError("cannot install retrieval network rule")
        if library.seccomp_load(context) != 0:
            raise RuntimeError("cannot activate retrieval network filter")
    finally:
        library.seccomp_release(context)
    return "linux_seccomp_no_non_unix_socket_or_connect"


class RecordingIndex:
    """Observe the public candidate boundary, including swallowed production errors."""

    def __init__(self, index, corpus):
        import hashlib

        self.index = index
        self.expected = {hashlib.sha256(("user\0" + c["key"]).encode()).hexdigest() for c in corpus}
        self.error_type, self.candidates, self.calls = None, None, 0

    async def search(self, **kwargs):
        self.calls += 1
        try:
            if {r["identity"] for r in kwargs["records"]} != self.expected:
                raise ValueError("production active corpus differs from frozen corpus")
            self.candidates = await self.index.search(**kwargs)
            return self.candidates
        except Exception as error:
            self.error_type = type(error).__name__
            raise


def _store(path, corpus, index=None):
    from my_agent_core.memory import MemoryStore
    from my_agent_core.memory_index import Fact, save_fact

    for chunk in corpus:
        save_fact(
            path / "facts",
            Fact(
                subject="user",
                key=chunk["key"],
                value=chunk_value(chunk),
                valid_from=chunk["valid_from"],
                importance=0.6,
                source="historical_session:" + chunk["session_id"],
            ),
        )
    store = MemoryStore(path, memory_index=index)
    store.load_from_disk()
    return store


def _close_fts(store):
    connection = getattr(getattr(getattr(store, "_index", None), "_fts", None), "_conn", None)
    if connection is not None:
        connection.close()


async def retrieve_question(
    question,
    corpus,
    workdir,
    *,
    index_factory=None,
    model_dir=None,
    budget_chars=6000,
    index_timeout=1800,
    close_timeout=60,
    threads=2,
):
    if type(budget_chars) is not int or not 1 <= budget_chars <= 6000:
        raise ValueError("context budget must be in 1..6000")
    workdir = Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    results = []
    selection_budget = 8 * (max(len(chunk_value(c)) for c in corpus) + 256)
    for arm in ARMS:
        started = time.monotonic()
        row = {
            "question_id": question["question_id"],
            "arm": arm,
            "valid": True,
            "error_type": None,
            "context": "",
            "chunk_keys": [],
            "session_ids": [],
            "truncated": False,
            "context_chars": 0,
            "top_k_chunks": 8,
            "budget_chars": budget_chars,
            "corpus_chunks": len(corpus),
            "close_complete": True,
            "index_init_seconds": 0.0,
            "store_build_seconds": 0.0,
            "index_and_query_seconds": 0.0,
        }
        if arm == "none":
            row.update(empty=True, total_seconds=time.monotonic() - started)
            results.append(row)
            continue
        index, store = None, None
        with tempfile.TemporaryDirectory(prefix=arm + "-", dir=workdir) as temporary:
            directory = Path(temporary)
            try:
                capture = None
                if arm == "mem0":
                    init = time.monotonic()
                    if index_factory is None:
                        from my_agent_core.memory_local import local_cpu_config
                        from my_agent_core.memory_mem0 import create_mem0_index

                        index = create_mem0_index(
                            local_cpu_config(model_dir=model_dir, storage_dir=directory / "vectors", threads=threads),
                            timeout=index_timeout,
                        )
                    else:
                        index = index_factory(
                            model_dir=model_dir, storage_dir=directory / "vectors", timeout=index_timeout
                        )
                    index.top_k = 8
                    capture = RecordingIndex(index, corpus)
                    row["index_init_seconds"] = time.monotonic() - init
                build = time.monotonic()
                store = _store(directory / "store", corpus, capture)
                row["store_build_seconds"] = time.monotonic() - build
                query = time.monotonic()
                if arm == "lexical":
                    text = store.retrieve(question["question"], budget_chars=selection_budget) or ""
                    keys = re.findall(r'^- subject="user" key="(lme_[a-f0-9]{24})" value=', text, flags=re.MULTILINE)
                    row["backend"] = store._index.backend
                else:
                    text = await store.aretrieve(question["question"], budget_chars=selection_budget) or ""
                    row["index_status"] = dict(index.status)
                    if capture.error_type:
                        row.update(valid=False, error_type=capture.error_type)
                        keys = []
                    elif capture.candidates == []:
                        keys = []  # suppress the production lexical fallback for a legitimate empty result
                        row["empty_fallback_suppressed"] = bool(text)
                    elif capture.calls != 1 or not text.startswith("<retrieved_memory>"):
                        row.update(valid=False, error_type="SemanticFallbackDetected")
                        keys = []
                    else:
                        keys = re.findall(r"^- (lme_[a-f0-9]{24}): ", text, flags=re.MULTILINE)
                        identity_to_key = {
                            __import__("hashlib").sha256(("user\0" + c["key"]).encode()).hexdigest(): c["key"]
                            for c in corpus
                        }
                        permitted = {identity_to_key.get(c.get("identity")) for c in capture.candidates}
                        if any(k not in permitted for k in keys):
                            raise ValueError("semantic context does not match candidate identities")
                row["index_and_query_seconds"] = time.monotonic() - query
                if row["valid"]:
                    row.update(render_chunks(corpus, list(dict.fromkeys(keys)), budget_chars))
            except Exception as error:
                row.update(
                    valid=False,
                    error_type=type(error).__name__,
                    context="",
                    context_chars=0,
                    chunk_keys=[],
                    session_ids=[],
                )
            finally:
                if store is not None:
                    _close_fts(store)
                if index is not None:
                    row["close_complete"] = await index.aclose(timeout=close_timeout)
                    if not row["close_complete"]:
                        row.update(
                            valid=False,
                            error_type="IncompleteIndexClose",
                            context="",
                            context_chars=0,
                            chunk_keys=[],
                            session_ids=[],
                        )
        row.update(empty=not row["chunk_keys"], total_seconds=time.monotonic() - started)
        results.append(row)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", required=True, type=Path)
    parser.add_argument("--corpus", required=True, type=Path)
    parser.add_argument("--model-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--budget-chars", type=int, default=6000)
    parser.add_argument("--index-timeout", type=float, default=1800)
    parser.add_argument("--close-timeout", type=float, default=60)
    parser.add_argument("--threads", type=int, default=2)
    args = parser.parse_args(argv)
    os.environ.update(
        MEM0_TELEMETRY="false", HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1"
    )
    boundary = deny_network()
    question, corpus = json.loads(args.question.read_bytes()), json.loads(args.corpus.read_bytes())
    with tempfile.TemporaryDirectory(prefix="lme-retrieval-") as work:
        rows = asyncio.run(
            retrieve_question(
                question,
                corpus,
                work,
                model_dir=args.model_dir,
                budget_chars=args.budget_chars,
                index_timeout=args.index_timeout,
                close_timeout=args.close_timeout,
                threads=args.threads,
            )
        )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump({"network_boundary": boundary, "rows": rows}, stream, ensure_ascii=False, allow_nan=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
