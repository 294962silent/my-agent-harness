"""Immutable manifests, hash-chained journals, snapshots and checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import threading
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, BinaryIO

from .schema import canonical_json, digest_value, parse_json_object, parse_role_episode
from .state import (
    EVENT_FIELDS,
    EVENT_KINDS,
    ResumeBlockedError,
    RunState,
    StateIntegrityError,
    assert_resume_allowed,
    reduce_events,
    validate_events,
)

_DEFAULT_SNAPSHOT_MAX_FILES = 5000
_DEFAULT_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
_IGNORED_COMPONENTS = frozenset({".git", ".venv", "__pycache__", ".pytest_cache"})
_FILE_ATTRIBUTE_REPARSE_POINT = getattr(
    stat,
    "FILE_ATTRIBUTE_REPARSE_POINT",
    0x400,
)
_SESSION_NAME_RE = re.compile(
    r"^round_(?P<round>[0-9]{4})_(?P<role>manager|executor|auditor)"
    r"(?:_[0-9a-f]{12})?\.jsonl$"
)
_AUDITOR_EVIDENCE_TOOLS = frozenset({"read", "grep", "find", "ls", "bash"})
_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "access_token",
        "token",
        "secret",
        "secret_key",
        "password",
        "credential",
    }
)


class StoreError(RuntimeError):
    """Persistent state cannot be read or safely updated."""


class StoreIntegrityError(StoreError):
    """Stored bytes violate the immutable state contract."""


@dataclass(frozen=True)
class SnapshotEntry:
    path: str
    sha256: str
    size: int
    mode: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class WorkspaceSnapshot:
    sha256: str
    path: Path
    file_count: int
    byte_count: int
    files: tuple[SnapshotEntry, ...]

    @property
    def digest(self) -> str:
        return self.sha256

    def to_dict(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "path": self.path.as_posix(),
            "file_count": self.file_count,
            "byte_count": self.byte_count,
        }


def _is_link(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        if bool(getattr(path, "is_junction", lambda: False)()):
            return True
        if os.name == "nt":
            metadata = os.lstat(path)
            attributes = int(getattr(metadata, "st_file_attributes", 0))
            if attributes & _FILE_ATTRIBUTE_REPARSE_POINT:
                return True
        return False
    except OSError:
        return True


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except (OSError, ValueError):
        return False
    return True


def _assert_safe_under(root: Path, path: Path, *, allow_missing: bool) -> Path:
    root = root.resolve()
    candidate = path if path.is_absolute() else root / path
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        # The lexical path is still checked after resolution below.
        relative = None
    current = root
    if _is_link(root):
        raise StoreIntegrityError(f"managed root is a link: {root}")
    if relative is not None:
        for component in relative.parts:
            current = current / component
            if current.exists() and _is_link(current):
                raise StoreIntegrityError(f"path contains a link: {current}")
    resolved = candidate.resolve(strict=False)
    if resolved != root and not _inside(resolved, root):
        raise StoreIntegrityError(f"path escapes managed root: {candidate}")
    if not allow_missing and not resolved.exists():
        raise StoreIntegrityError(f"managed path is missing: {resolved}")
    return resolved


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and _is_link(path):
        raise StoreIntegrityError(f"refusing to replace link: {path}")
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary.exists():
            temporary.unlink()


def _read_bytes_nofollow(path: Path) -> bytes:
    if _is_link(path):
        raise StoreIntegrityError(f"refusing to read link: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StoreIntegrityError(f"cannot open file safely: {path}") from exc
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise StoreIntegrityError(f"not a regular file: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


class _FileLock:
    def __init__(self, path: Path, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout
        self.handle: BinaryIO | None = None

    def __enter__(self) -> _FileLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists() and _is_link(self.path):
            raise StoreIntegrityError(f"lock path is a link: {self.path}")
        self.handle = self.path.open("a+b")
        self.handle.seek(0, os.SEEK_END)
        if self.handle.tell() == 0:
            self.handle.write(b"\0")
            self.handle.flush()
            os.fsync(self.handle.fileno())
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    self.handle.seek(0)
                    msvcrt.locking(self.handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    self.handle.close()
                    self.handle = None
                    raise StoreError(f"timed out acquiring store lock: {self.path}") from None
                time.sleep(0.01)

    def __exit__(self, exc_type, exc, tb) -> None:
        handle = self.handle
        self.handle = None
        if handle is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _json_copy(value: Mapping[str, Any], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise StoreIntegrityError(f"{name} must be an object")
    try:
        copied = json.loads(canonical_json(dict(value)))
    except (TypeError, ValueError) as exc:
        raise StoreIntegrityError(f"{name} must be canonical-JSON serializable") from exc
    if not isinstance(copied, dict):
        raise StoreIntegrityError(f"{name} must be an object")
    return copied


def _reject_secret_keys(value: object, *, path: str = "") -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _SECRET_KEYS or normalized.endswith("_api_key"):
                raise StoreIntegrityError(f"manifest may not persist credential field: {path}{key}")
            _reject_secret_keys(item, path=f"{path}{key}.")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_secret_keys(item, path=f"{path}{index}.")


def _safe_component(value: object, name: str) -> str:
    text = str(value)
    if (
        not text
        or len(text) > 128
        or text in {".", ".."}
        or "/" in text
        or "\\" in text
        or any(ord(char) < 0x20 or ord(char) == 0x7F for char in text)
    ):
        raise StoreIntegrityError(f"{name} is not a safe path component")
    return text


def _normalize_immutable_paths(
    workspace: Path | None,
    values: Sequence[str],
) -> list[str]:
    """Return sorted, workspace-relative paths in their canonical text form."""

    if isinstance(values, (str, bytes, bytearray)):
        raise StoreIntegrityError("immutable paths must be an iterable of path strings")
    try:
        raw_values = list(values)
    except TypeError as exc:
        raise StoreIntegrityError("immutable paths must be an iterable of path strings") from exc
    normalized: set[str] = set()
    for value in raw_values:
        if not isinstance(value, str):
            raise StoreIntegrityError("immutable path must be text")
        text = value
        if not text.strip():
            raise StoreIntegrityError("immutable path must not be empty")
        windows_candidate = PureWindowsPath(text)
        if windows_candidate.is_absolute() or windows_candidate.drive:
            raise StoreIntegrityError("immutable path must be workspace-relative")
        candidate = PurePosixPath(text.replace("\\", "/"))
        if candidate.is_absolute():
            raise StoreIntegrityError("immutable path must be workspace-relative")
        parts = tuple(part for part in candidate.parts if part not in {"", "."})
        if not parts or any(part == ".." for part in parts):
            raise StoreIntegrityError("immutable path escapes the workspace")
        relative = Path(*parts)
        if workspace is None:
            normalized.add(relative.as_posix())
            continue
        try:
            target = _assert_safe_under(
                workspace,
                workspace / relative,
                allow_missing=False,
            )
        except (OSError, ValueError, StoreIntegrityError) as exc:
            raise StoreIntegrityError(f"immutable path is invalid: {text}") from exc
        if not (target.is_file() or target.is_dir()):
            raise StoreIntegrityError(f"immutable path is not a file or directory: {text}")
        normalized.add(target.relative_to(workspace).as_posix())
    return sorted(normalized)


def _file_sha_nofollow(path: Path, *, max_bytes: int) -> tuple[str, int, int]:
    if _is_link(path):
        raise StoreIntegrityError(f"workspace contains a link: {path}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise StoreIntegrityError(f"cannot read workspace file: {path}") from exc
    digest = hashlib.sha256()
    size = 0
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise StoreIntegrityError(f"workspace entry is not a regular file: {path}")
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise StoreIntegrityError(f"workspace file exceeds snapshot limit: {path}")
            digest.update(chunk)
        return digest.hexdigest(), size, stat.S_IMODE(metadata.st_mode)
    finally:
        os.close(descriptor)


class LongHorizonStore:
    """Durable storage for one long-horizon run."""

    def __init__(self, root: Path, manifest: Mapping[str, Any]):
        self.root = root.resolve()
        self._manifest = _json_copy(manifest, "manifest")
        self._thread_lock = threading.RLock()
        self._local = threading.local()
        self._controller_thread_lock = threading.RLock()
        if _is_link(self.root) or not self.root.is_dir():
            raise StoreIntegrityError(f"store root must be a real directory: {self.root}")

    @property
    def manifest(self) -> dict[str, Any]:
        return deepcopy(self._manifest)

    @property
    def workspace(self) -> Path:
        return Path(self._manifest["workspace"])

    def _path(self, *parts: str, allow_missing: bool = True) -> Path:
        candidate = self.root.joinpath(*parts)
        return _assert_safe_under(self.root, candidate, allow_missing=allow_missing)

    @contextmanager
    def _lock(self) -> Iterator[None]:
        with self._thread_lock:
            depth = int(getattr(self._local, "lock_depth", 0))
            if depth:
                self._local.lock_depth = depth + 1
                try:
                    yield
                finally:
                    self._local.lock_depth = depth
                return
            with _FileLock(self._path(".lock")):
                self._local.lock_depth = 1
                try:
                    yield
                finally:
                    self._local.lock_depth = 0

    @contextmanager
    def controller_lock(self):
        """Serialize a whole controller run, including model/tool execution."""

        with self._controller_thread_lock:
            with _FileLock(self._path(".controller.lock")):
                yield

    @classmethod
    def prepare(
        cls,
        root: Path,
        *,
        run_id: str,
        task: str,
        workspace: Path,
        model: Mapping[str, Any],
        limits: Mapping[str, Any],
        immutable_paths: Sequence[str] = (),
    ) -> LongHorizonStore:
        root = Path(root).expanduser().resolve()
        workspace = Path(workspace).expanduser().resolve()
        if not isinstance(task, str):
            raise StoreIntegrityError("task must be text")
        if not workspace.is_dir() or workspace.is_symlink():
            raise StoreIntegrityError("workspace must be an existing real directory")
        immutable_paths_copy = _normalize_immutable_paths(workspace, immutable_paths)
        if root == workspace or _inside(workspace, root):
            raise StoreIntegrityError("run root must be outside every snapshotted workspace subtree")
        safe_run_id = _safe_component(run_id, "run_id")
        model_copy = _json_copy(model, "model")
        limits_copy = _json_copy(limits, "limits")
        _reject_secret_keys(model_copy)
        _reject_secret_keys(limits_copy)
        limits_copy.setdefault("snapshot_max_files", _DEFAULT_SNAPSHOT_MAX_FILES)
        limits_copy.setdefault("snapshot_max_bytes", _DEFAULT_SNAPSHOT_MAX_BYTES)
        for field in ("snapshot_max_files", "snapshot_max_bytes"):
            if type(limits_copy.get(field)) is not int or limits_copy[field] < 1:
                raise StoreIntegrityError(f"limits.{field} must be a positive integer")
        if "max_rounds" in limits_copy and (
            type(limits_copy["max_rounds"]) is not int or limits_copy["max_rounds"] < 1
        ):
            raise StoreIntegrityError("limits.max_rounds must be a positive integer")
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            root.mkdir()
        except FileExistsError:
            raise StoreIntegrityError(f"nonzero/new run root is required: {root}") from None
        provisional = {
            "schema_version": 1,
            "run_id": safe_run_id,
            "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
            "workspace": str(workspace),
            "model": model_copy,
            "limits": limits_copy,
            "initial_workspace_sha256": "0" * 64,
            "immutable_paths": immutable_paths_copy,
        }
        probe = cls(root, provisional)
        _entries, initial_digest, _bytes = probe._workspace_inventory()
        manifest = {
            "schema_version": 1,
            "run_id": safe_run_id,
            "task_sha256": hashlib.sha256(task.encode("utf-8")).hexdigest(),
            "workspace": str(workspace),
            "model": model_copy,
            "limits": limits_copy,
            "initial_workspace_sha256": initial_digest,
            "immutable_paths": immutable_paths_copy,
        }
        _atomic_write(root / "manifest.json", canonical_json(manifest) + b"\n")
        _atomic_write(root / "task.txt", task.encode("utf-8"))
        _atomic_write(
            root / "status.json",
            canonical_json({"status": "ready", "next_round": 1}) + b"\n",
        )
        store = cls(root, manifest)
        store.append("prepared", {})
        return store

    @classmethod
    def load(cls, root: Path) -> LongHorizonStore:
        root = Path(root).expanduser().resolve()
        if not root.is_dir() or root.is_symlink():
            raise StoreIntegrityError("store root must be an existing real directory")
        manifest_path = _assert_safe_under(root, root / "manifest.json", allow_missing=False)
        try:
            value = json.loads(_read_bytes_nofollow(manifest_path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreIntegrityError("manifest.json is not valid UTF-8 JSON") from exc
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise StoreIntegrityError("unsupported manifest")
        required = {
            "schema_version",
            "run_id",
            "task_sha256",
            "workspace",
            "model",
            "limits",
            "initial_workspace_sha256",
        }
        fields = set(value)
        if fields != required and fields != required | {"immutable_paths"}:
            raise StoreIntegrityError("manifest fields differ from the contract")
        store = cls(root, value)
        if "immutable_paths" in value:
            if not isinstance(value["immutable_paths"], list):
                raise StoreIntegrityError("manifest immutable_paths must be a list")
            expected_immutable_paths = _normalize_immutable_paths(
                None,
                value["immutable_paths"],
            )
            if value["immutable_paths"] != expected_immutable_paths:
                raise StoreIntegrityError("manifest immutable_paths are not normalized")
        task_path = _assert_safe_under(root, root / "task.txt", allow_missing=False)
        task = _read_bytes_nofollow(task_path)
        if hashlib.sha256(task).hexdigest() != value["task_sha256"]:
            raise StoreIntegrityError("task.txt hash drifted")
        store.events()
        return store

    def _events_unlocked(self) -> list[dict[str, Any]]:
        path = self._path("journal.jsonl", allow_missing=False)
        content = _read_bytes_nofollow(path)
        if not content or not content.endswith(b"\n"):
            raise StoreIntegrityError("journal is empty or torn")
        rows: list[dict[str, Any]] = []
        for index, raw in enumerate(content.splitlines()):
            if not raw:
                raise StoreIntegrityError(f"journal contains blank line {index + 1}")
            try:
                row = parse_json_object(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError) as exc:
                raise StoreIntegrityError(f"journal line {index + 1} is invalid") from exc
            if set(row) != EVENT_FIELDS:
                raise StoreIntegrityError(f"journal line {index + 1} has invalid fields")
            rows.append(row)
        try:
            validated = validate_events(self._manifest, rows)
            reduce_events(self._manifest, validated)
            return validated
        except StateIntegrityError as exc:
            raise StoreIntegrityError(str(exc)) from exc

    def events(self) -> list[dict[str, Any]]:
        with self._lock():
            return deepcopy(self._events_unlocked())

    def append(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        if kind not in EVENT_KINDS:
            raise StoreIntegrityError(f"unknown journal event kind: {kind}")
        body = _json_copy(payload, "event payload")
        with self._lock():
            journal = self._path("journal.jsonl", allow_missing=True)
            events = self._events_unlocked() if journal.exists() else []
            row: dict[str, Any] = {
                "schema_version": 1,
                "sequence": len(events),
                "previous": events[-1]["sha256"] if events else None,
                "manifest_sha256": digest_value(self._manifest),
                "kind": kind,
                "payload": body,
                "time_unix": time.time(),
            }
            row["sha256"] = digest_value(row)
            try:
                prospective = validate_events(self._manifest, [*events, row])
                reduce_events(self._manifest, prospective)
            except StateIntegrityError as exc:
                raise StoreIntegrityError(f"append would invalidate journal: {exc}") from exc
            flags = (
                os.O_WRONLY
                | os.O_APPEND
                | os.O_CREAT
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            descriptor = os.open(journal, flags, 0o600)
            try:
                line = canonical_json(row) + b"\n"
                offset = 0
                while offset < len(data := line):
                    offset += os.write(descriptor, data[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            _fsync_directory(journal.parent)
            # Validate the line just written through the full chain before returning.
            self._events_unlocked()
            return deepcopy(row)

    def _workspace_inventory(self) -> tuple[tuple[SnapshotEntry, ...], str, int]:
        workspace = self.workspace
        if not workspace.is_dir() or _is_link(workspace):
            raise StoreIntegrityError("workspace must remain a real directory")
        run_root = self.root
        max_files = int(self._manifest["limits"]["snapshot_max_files"])
        max_bytes = int(self._manifest["limits"]["snapshot_max_bytes"])
        entries: list[SnapshotEntry] = []
        total_bytes = 0
        for current, dirs, files in os.walk(workspace, topdown=True, followlinks=False):
            current_path = Path(current)
            kept_dirs: list[str] = []
            for name in sorted(dirs):
                candidate = current_path / name
                if name in _IGNORED_COMPONENTS:
                    continue
                if _is_link(candidate):
                    raise StoreIntegrityError(f"workspace contains a linked directory: {candidate}")
                resolved = candidate.resolve(strict=False)
                if resolved == run_root or _inside(resolved, run_root):
                    continue
                kept_dirs.append(name)
            dirs[:] = kept_dirs
            for name in sorted(files):
                path = current_path / name
                if name in _IGNORED_COMPONENTS:
                    continue
                resolved = path.resolve(strict=False)
                if resolved == run_root or _inside(resolved, run_root):
                    continue
                if _is_link(path):
                    raise StoreIntegrityError(f"workspace contains a link: {path}")
                sha256, size, mode = _file_sha_nofollow(path, max_bytes=max_bytes)
                total_bytes += size
                if len(entries) + 1 > max_files:
                    raise StoreIntegrityError("workspace exceeds snapshot file limit")
                if total_bytes > max_bytes:
                    raise StoreIntegrityError("workspace exceeds snapshot byte limit")
                entries.append(
                    SnapshotEntry(
                        path=path.relative_to(workspace).as_posix(),
                        sha256=sha256,
                        size=size,
                        mode=mode,
                    )
                )
        entries.sort(key=lambda item: item.path)
        digest = digest_value([item.to_dict() for item in entries])
        return tuple(entries), digest, total_bytes

    def workspace_digest(self) -> str:
        with self._lock():
            _entries, digest, _bytes = self._workspace_inventory()
            return digest

    def _immutable_roots(self) -> tuple[str, ...]:
        paths = sorted(
            set(self._manifest.get("immutable_paths", [])),
            key=lambda value: (len(value.split("/")), value),
        )
        roots: list[str] = []
        for path in paths:
            if any(path == root or path.startswith(f"{root}/") for root in roots):
                continue
            roots.append(path)
        return tuple(roots)

    def _capture_immutable_state_unlocked(self) -> dict[str, Any]:
        records: list[dict[str, Any]] = []
        max_bytes = int(self._manifest["limits"]["snapshot_max_bytes"])
        for root_name in self._immutable_roots():
            try:
                root = _assert_safe_under(
                    self.workspace,
                    self.workspace / root_name,
                    allow_missing=False,
                )
            except StoreIntegrityError as exc:
                raise StoreIntegrityError(
                    f"immutable path changed or disappeared: {root_name}"
                ) from exc
            if _is_link(root):
                raise StoreIntegrityError(f"immutable path contains a link: {root_name}")
            root_mode = stat.S_IMODE(os.lstat(root).st_mode)
            root_relative = root.relative_to(self.workspace).as_posix()
            if root.is_file():
                sha256, size, mode = _file_sha_nofollow(root, max_bytes=max_bytes)
                records.append(
                    {
                        "root": root_name,
                        "path": root_relative,
                        "kind": "file",
                        "sha256": sha256,
                        "size": size,
                        "mode": mode,
                    }
                )
                continue
            if not root.is_dir():
                raise StoreIntegrityError(
                    f"immutable path is not a file or directory: {root_name}"
                )
            records.append(
                {
                    "root": root_name,
                    "path": root_relative,
                    "kind": "directory",
                    "mode": root_mode,
                }
            )
            for current, dirs, files in os.walk(root, topdown=True, followlinks=False):
                current_path = Path(current)
                dirs.sort()
                files.sort()
                kept_dirs: list[str] = []
                for name in dirs:
                    if name in _IGNORED_COMPONENTS:
                        continue
                    directory = current_path / name
                    if _is_link(directory):
                        raise StoreIntegrityError(
                            f"immutable path contains a link: {root_name}"
                        )
                    metadata = os.lstat(directory)
                    if not stat.S_ISDIR(metadata.st_mode):
                        raise StoreIntegrityError(
                            f"immutable directory entry changed: {root_name}"
                        )
                    records.append(
                        {
                            "root": root_name,
                            "path": directory.relative_to(self.workspace).as_posix(),
                            "kind": "directory",
                            "mode": stat.S_IMODE(metadata.st_mode),
                        }
                    )
                    kept_dirs.append(name)
                dirs[:] = kept_dirs
                for name in files:
                    if name in _IGNORED_COMPONENTS:
                        continue
                    file_path = current_path / name
                    sha256, size, mode = _file_sha_nofollow(
                        file_path,
                        max_bytes=max_bytes,
                    )
                    records.append(
                        {
                            "root": root_name,
                            "path": file_path.relative_to(self.workspace).as_posix(),
                            "kind": "file",
                            "sha256": sha256,
                            "size": size,
                            "mode": mode,
                        }
                    )
        records.sort(key=lambda item: item["path"])
        return {"records": records}

    def _snapshot_immutable_records(
        self,
        snapshot: WorkspaceSnapshot,
    ) -> list[dict[str, Any]]:
        roots = self._immutable_roots()
        return [
            {
                "path": entry.path,
                "sha256": entry.sha256,
                "size": entry.size,
                "mode": entry.mode,
            }
            for entry in snapshot.files
            if any(entry.path == root or entry.path.startswith(f"{root}/") for root in roots)
        ]

    def capture_immutable_state(self) -> dict[str, Any]:
        """Capture protected subtrees immediately before Executor work."""

        with self._lock():
            return self._capture_immutable_state_unlocked()

    def assert_immutable_unchanged(
        self,
        before: Mapping[str, Any],
        *,
        snapshot: WorkspaceSnapshot | None = None,
    ) -> None:
        """Fail closed when Executor changed an immutable path's bytes or type."""

        with self._lock():
            expected = _json_copy(before, "immutable state")
            actual = self._capture_immutable_state_unlocked()
            if canonical_json(expected) != canonical_json(actual):
                changed = sorted(
                    {
                        record["root"]
                        for record in [
                            *expected.get("records", []),
                            *actual.get("records", []),
                        ]
                        if isinstance(record, Mapping) and isinstance(record.get("root"), str)
                    }
                )
                detail = ", ".join(changed) if changed else "unknown"
                raise StoreIntegrityError(f"immutable path changed: {detail}")
            if snapshot is None:
                return
            if not isinstance(snapshot, WorkspaceSnapshot):
                raise StoreIntegrityError("immutable snapshot must be WorkspaceSnapshot")
            snapshot_entries = [entry.to_dict() for entry in snapshot.files]
            if (
                digest_value(snapshot_entries) != snapshot.sha256
                or len(snapshot.files) != snapshot.file_count
                or sum(entry.size for entry in snapshot.files) != snapshot.byte_count
            ):
                raise StoreIntegrityError("immutable snapshot metadata is inconsistent")
            expected_files = [
                {
                    "path": record["path"],
                    "sha256": record["sha256"],
                    "size": record["size"],
                    "mode": record["mode"],
                }
                for record in expected.get("records", [])
                if isinstance(record, Mapping) and record.get("kind") == "file"
            ]
            snapshot_files = self._snapshot_immutable_records(snapshot)
            if canonical_json(expected_files) != canonical_json(snapshot_files):
                raise StoreIntegrityError(
                    "immutable path changed in Executor snapshot"
                )

    def _read_session_rows(self, path: Path) -> list[dict[str, Any]]:
        payload = _read_bytes_nofollow(path)
        if not payload:
            raise StoreIntegrityError("role session file must be nonempty")
        if not payload.endswith(b"\n"):
            raise StoreIntegrityError("role session must be newline-terminated JSONL")
        rows: list[dict[str, Any]] = []
        try:
            lines = payload.decode("utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise StoreIntegrityError("role session must be valid UTF-8 JSONL") from exc
        for index, line in enumerate(lines, start=1):
            if not line:
                raise StoreIntegrityError(f"role session line {index} must not be blank")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise StoreIntegrityError(
                    f"role session is not valid JSONL at line {index}"
                ) from exc
            if not isinstance(row, dict):
                raise StoreIntegrityError(
                    f"role session line {index} must be a JSON object"
                )
            rows.append(row)
        return rows

    def validate_session_path(
        self,
        value: str,
        *,
        expected_role: str | None = None,
        expected_round: int | None = None,
    ) -> Path:
        raw = Path(value).expanduser()
        candidate = raw if raw.is_absolute() else self.root / raw
        path = _assert_safe_under(self.root, candidate, allow_missing=False)
        if not path.is_file() or _is_link(path):
            raise StoreIntegrityError("role session must be a real file inside the run root")
        sessions_root = self._path("sessions", allow_missing=True)
        if path.parent.resolve() != sessions_root.resolve():
            raise StoreIntegrityError("role session must be directly inside run/sessions")
        match = _SESSION_NAME_RE.fullmatch(path.name)
        if match is None:
            raise StoreIntegrityError(
                "role session name must match its role and round"
            )
        if (
            expected_role is not None
            and match.group("role") != expected_role
        ):
            raise StoreIntegrityError(
                "role session name does not match its role and round"
            )
        if (
            expected_round is not None
            and int(match.group("round")) != expected_round
        ):
            raise StoreIntegrityError(
                "role session name does not match its role and round"
            )
        self._read_session_rows(path)
        return path

    def validate_auditor_tool_evidence(self, value: str) -> Path:
        path = self.validate_session_path(value, expected_role="auditor")
        rows = self._read_session_rows(path)
        for row in rows:
            message = row.get("message")
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            metadata = message.get("metadata")
            if not isinstance(metadata, dict):
                continue
            if (
                metadata.get("tool_name") in _AUDITOR_EVIDENCE_TOOLS
                and metadata.get("is_error") is False
            ):
                return path
        raise StoreIntegrityError(
            "auditor session requires at least one successful allowed tool result"
        )

    def task_text(self) -> str:
        path = self._path("task.txt", allow_missing=False)
        try:
            text = _read_bytes_nofollow(path).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise StoreIntegrityError("task.txt is not valid UTF-8") from exc
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != self._manifest["task_sha256"]:
            raise StoreIntegrityError("task.txt hash drifted")
        return text

    def write_inbox(self, entries: list[Mapping[str, str]]) -> None:
        normalized = []
        for entry in entries:
            if set(entry) != {"follow_up_id", "text"}:
                raise StoreIntegrityError("inbox entry fields differ")
            follow_up_id = entry.get("follow_up_id")
            text = entry.get("text")
            if not isinstance(follow_up_id, str) or not follow_up_id or not isinstance(text, str):
                raise StoreIntegrityError("inbox entry is invalid")
            normalized.append({"follow_up_id": follow_up_id, "text": text})
        with self._lock():
            target = self._path("inbox.json")
            _atomic_write(target, canonical_json({"entries": normalized}) + b"\n")

    def read_inbox(self) -> list[dict[str, str]]:
        path = self._path("inbox.json", allow_missing=True)
        if not path.exists():
            return []
        try:
            value = json.loads(_read_bytes_nofollow(path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreIntegrityError("inbox.json is invalid") from exc
        entries = value.get("entries") if isinstance(value, dict) else None
        if not isinstance(entries, list):
            raise StoreIntegrityError("inbox.json schema is invalid")
        result = []
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"follow_up_id", "text"}
                or not isinstance(entry["follow_up_id"], str)
                or not isinstance(entry["text"], str)
            ):
                raise StoreIntegrityError("inbox.json entry is invalid")
            result.append(dict(entry))
        return result

    def _snapshot_path(self, digest: str, *, allow_missing: bool = True) -> Path:
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise StoreIntegrityError("snapshot id must be a lowercase SHA-256")
        return self._path("snapshots", digest, allow_missing=allow_missing)

    def _verify_snapshot(self, digest: str) -> Path:
        path = self._snapshot_path(digest, allow_missing=False)
        metadata_path = _assert_safe_under(path, path / "snapshot.json", allow_missing=False)
        try:
            metadata = json.loads(_read_bytes_nofollow(metadata_path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreIntegrityError("snapshot metadata is invalid") from exc
        if not isinstance(metadata, dict) or metadata.get("sha256") != digest:
            raise StoreIntegrityError("snapshot metadata identity differs")
        if set(metadata) != {"schema_version", "sha256", "file_count", "byte_count", "files"}:
            raise StoreIntegrityError("snapshot metadata fields differ")
        if metadata.get("schema_version") != 1:
            raise StoreIntegrityError("unsupported snapshot metadata")
        tree = _assert_safe_under(path, path / "tree", allow_missing=False)
        entries: list[SnapshotEntry] = []
        total = 0
        max_files = int(self._manifest["limits"]["snapshot_max_files"])
        max_bytes = int(self._manifest["limits"]["snapshot_max_bytes"])
        for current, dirs, files in os.walk(tree, topdown=True, followlinks=False):
            current_path = Path(current)
            for name in dirs:
                if _is_link(current_path / name):
                    raise StoreIntegrityError("snapshot contains a linked directory")
            for name in sorted(files):
                candidate = current_path / name
                sha, size, mode = _file_sha_nofollow(candidate, max_bytes=max_bytes)
                total += size
                if len(entries) + 1 > max_files or total > max_bytes:
                    raise StoreIntegrityError("stored snapshot exceeds bounds")
                entries.append(
                    SnapshotEntry(
                        path=candidate.relative_to(tree).as_posix(),
                        sha256=sha,
                        size=size,
                        mode=mode,
                    )
                )
        entries.sort(key=lambda item: item.path)
        actual = digest_value([item.to_dict() for item in entries])
        if (
            actual != digest
            or metadata.get("files") != [item.to_dict() for item in entries]
            or metadata.get("file_count") != len(entries)
            or metadata.get("byte_count") != total
        ):
            raise StoreIntegrityError("snapshot content hash drifted")
        return tree

    def capture_workspace(self) -> WorkspaceSnapshot:
        with self._lock():
            entries, digest, total = self._workspace_inventory()
            target = self._snapshot_path(digest)
            if target.exists():
                self._verify_snapshot(digest)
            else:
                snapshots_root = self._path("snapshots")
                snapshots_root.mkdir(parents=True, exist_ok=True)
                staging = _assert_safe_under(
                    self.root,
                    snapshots_root / f".tmp-{uuid.uuid4().hex[:12]}",
                    allow_missing=True,
                )
                staging.mkdir()
                tree = staging / "tree"
                tree.mkdir()
                for entry in entries:
                    source = _assert_safe_under(
                        self.workspace,
                        self.workspace / entry.path,
                        allow_missing=False,
                    )
                    destination = _assert_safe_under(tree, tree / entry.path, allow_missing=True)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    payload = _read_bytes_nofollow(source)
                    if hashlib.sha256(payload).hexdigest() != entry.sha256:
                        raise StoreIntegrityError("workspace changed during snapshot capture")
                    _atomic_write(destination, payload, mode=entry.mode)
                after_entries, after_digest, after_total = self._workspace_inventory()
                if (
                    after_entries != entries
                    or after_digest != digest
                    or after_total != total
                ):
                    shutil.rmtree(staging, ignore_errors=True)
                    raise StoreIntegrityError("workspace changed during snapshot capture")
                metadata = {
                    "schema_version": 1,
                    "sha256": digest,
                    "file_count": len(entries),
                    "byte_count": total,
                    "files": [entry.to_dict() for entry in entries],
                }
                _atomic_write(staging / "snapshot.json", canonical_json(metadata) + b"\n")
                try:
                    os.replace(staging, target)
                except OSError as exc:
                    if not target.exists():
                        raise StoreError("could not publish workspace snapshot") from exc
                    shutil.rmtree(staging, ignore_errors=True)
                self._verify_snapshot(digest)
            return WorkspaceSnapshot(
                sha256=digest,
                path=target,
                file_count=len(entries),
                byte_count=total,
                files=entries,
            )

    def create_audit_workspace(
        self,
        round_index: int,
        snapshot: WorkspaceSnapshot,
    ) -> Path:
        """Materialize one immutable Executor-after snapshot for the Auditor."""

        if type(round_index) is not int or round_index < 1:
            raise StoreIntegrityError("audit workspace round_index must be positive")
        if not isinstance(snapshot, WorkspaceSnapshot):
            raise StoreIntegrityError("audit workspace requires WorkspaceSnapshot")
        with self._lock():
            tree = self._verify_snapshot(snapshot.sha256)
            entries, digest, total = self._inventory_free_path(
                tree,
                ignore_path=None,
            )
            if (
                digest != snapshot.sha256
                or len(entries) != snapshot.file_count
                or total != snapshot.byte_count
            ):
                raise StoreIntegrityError(
                    "audit workspace snapshot differs from its verified content"
                )
            audit_root = self._path("audit-workspaces", allow_missing=True)
            audit_root.mkdir(parents=True, exist_ok=True)
            if _is_link(audit_root):
                raise StoreIntegrityError("audit workspace root must not be a link")
            target = self._path(
                "audit-workspaces",
                f"round_{round_index:04d}",
                allow_missing=True,
            )
            if target.exists():
                raise StoreIntegrityError(
                    f"audit workspace already exists: {target}"
                )
            staging = self._path(
                "audit-workspaces",
                f".tmp-{uuid.uuid4().hex[:12]}",
                allow_missing=True,
            )
            staging.mkdir()
            try:
                for entry in entries:
                    source = _assert_safe_under(
                        tree,
                        tree / entry.path,
                        allow_missing=False,
                    )
                    destination = _assert_safe_under(
                        staging,
                        staging / entry.path,
                        allow_missing=True,
                    )
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    _atomic_write(
                        destination,
                        _read_bytes_nofollow(source),
                        mode=entry.mode,
                    )
                copied_entries, copied_digest, copied_total = (
                    self._inventory_free_path(staging, ignore_path=None)
                )
                if (
                    copied_entries != entries
                    or copied_digest != snapshot.sha256
                    or copied_total != snapshot.byte_count
                ):
                    raise StoreIntegrityError(
                        "audit workspace copy differs from Executor snapshot"
                    )
                os.replace(staging, target)
                _fsync_directory(target.parent)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            return target

    def set_status(self, value: Mapping[str, Any]) -> None:
        status = _json_copy(value, "status")
        with self._lock():
            target = self._path("status.json")
            _atomic_write(target, canonical_json(status) + b"\n")

    def write_checkpoint(
        self,
        round_index: int,
        state: Mapping[str, Any],
        snapshot: WorkspaceSnapshot,
    ) -> dict[str, Any]:
        if type(round_index) is not int or round_index < 1:
            raise StoreIntegrityError("checkpoint round_index must be positive")
        if not isinstance(snapshot, WorkspaceSnapshot):
            raise StoreIntegrityError("checkpoint requires WorkspaceSnapshot")
        state_copy = _json_copy(state, "checkpoint state")
        with self._lock():
            events = self._events_unlocked()
            completed_positions = {
                event["payload"]["round"]: index
                for index, event in enumerate(events)
                if event["kind"] == "round_completed"
            }
            if round_index not in completed_positions:
                raise StoreIntegrityError("checkpoint round is not completed")
            journal_events = completed_positions[round_index] + 1
            prefix = events[:journal_events]
            try:
                reduced = reduce_events(self._manifest, prefix)
            except StateIntegrityError as exc:
                raise StoreIntegrityError(str(exc)) from exc
            if canonical_json(state_copy) != canonical_json(asdict(reduced)):
                raise StoreIntegrityError(
                    "round checkpoint state is not the round-prefix reducer output"
                )
            self._verify_snapshot(snapshot.sha256)
            metadata_path = _assert_safe_under(
                self._snapshot_path(snapshot.sha256),
                self._snapshot_path(snapshot.sha256) / "snapshot.json",
                allow_missing=False,
            )
            try:
                metadata = json.loads(_read_bytes_nofollow(metadata_path).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StoreIntegrityError("snapshot metadata is invalid") from exc
            if (
                metadata.get("file_count") != snapshot.file_count
                or metadata.get("byte_count") != snapshot.byte_count
            ):
                raise StoreIntegrityError("WorkspaceSnapshot counts differ from stored metadata")
            body = {
                "schema_version": 1,
                "round_index": round_index,
                "manifest_sha256": digest_value(self._manifest),
                "journal_head_sha256": prefix[-1]["sha256"],
                "journal_events": journal_events,
                "snapshot": {
                    "sha256": snapshot.sha256,
                    "path": self._snapshot_path(snapshot.sha256).relative_to(self.root).as_posix(),
                    "file_count": snapshot.file_count,
                    "byte_count": snapshot.byte_count,
                },
                "state": state_copy,
            }
            payload = canonical_json(body) + b"\n"
            checksum = hashlib.sha256(payload).hexdigest()
            checkpoint = self._path("checkpoints", f"{round_index:04d}.json")
            sidecar = self._path("checkpoints", f"{round_index:04d}.sha256")
            _atomic_write(checkpoint, payload)
            _atomic_write(sidecar, (checksum + "\n").encode("ascii"))
            return {
                **body,
                "checkpoint_sha256": checksum,
                "checkpoint_path": str(checkpoint),
            }

    def write_final_checkpoint(
        self,
        state: Mapping[str, Any],
        snapshot: WorkspaceSnapshot,
    ) -> dict[str, Any]:
        """Write the complete reducer state and final workspace state."""

        if not isinstance(snapshot, WorkspaceSnapshot):
            raise StoreIntegrityError("final checkpoint requires WorkspaceSnapshot")
        state_copy = _json_copy(state, "final checkpoint state")
        with self._lock():
            events = self._events_unlocked()
            try:
                reduced = reduce_events(self._manifest, events)
            except StateIntegrityError as exc:
                raise StoreIntegrityError(str(exc)) from exc
            if reduced.status != "complete":
                raise StoreIntegrityError("final checkpoint requires complete reducer state")
            if canonical_json(state_copy) != canonical_json(asdict(reduced)):
                raise StoreIntegrityError("final checkpoint state is not the reducer output")
            self._verify_snapshot(snapshot.sha256)
            _entries, current_digest, _bytes = self._workspace_inventory()
            if current_digest != snapshot.sha256:
                raise StoreIntegrityError("final checkpoint snapshot differs from workspace")
            body = {
                "schema_version": 1,
                "kind": "final",
                "manifest_sha256": digest_value(self._manifest),
                "journal_head_sha256": events[-1]["sha256"],
                "journal_events": len(events),
                "snapshot": {
                    "sha256": snapshot.sha256,
                    "path": self._snapshot_path(snapshot.sha256).relative_to(self.root).as_posix(),
                    "file_count": snapshot.file_count,
                    "byte_count": snapshot.byte_count,
                },
                "state": state_copy,
            }
            payload = canonical_json(body) + b"\n"
            checksum = hashlib.sha256(payload).hexdigest()
            checkpoint = self._path("checkpoints", "final.json")
            sidecar = self._path("checkpoints", "final.sha256")
            _atomic_write(checkpoint, payload)
            _atomic_write(sidecar, (checksum + "\n").encode("ascii"))
            return {
                **body,
                "checkpoint_sha256": checksum,
                "checkpoint_path": str(checkpoint),
            }

    def _load_checkpoint_unlocked(self, round_index: int) -> dict[str, Any]:
        if type(round_index) is not int or round_index < 1:
            raise StoreIntegrityError("checkpoint round_index must be positive")
        checkpoint = self._path(
            "checkpoints",
            f"{round_index:04d}.json",
            allow_missing=False,
        )
        sidecar = self._path(
            "checkpoints",
            f"{round_index:04d}.sha256",
            allow_missing=False,
        )
        payload = _read_bytes_nofollow(checkpoint)
        try:
            expected = _read_bytes_nofollow(sidecar).decode("ascii").strip()
        except UnicodeDecodeError as exc:
            raise StoreIntegrityError("checkpoint sidecar is invalid") from exc
        checksum = hashlib.sha256(payload).hexdigest()
        if checksum != expected:
            raise StoreIntegrityError("checkpoint file hash drifted")
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreIntegrityError("checkpoint JSON is invalid") from exc
        required = {
            "schema_version",
            "round_index",
            "manifest_sha256",
            "journal_head_sha256",
            "journal_events",
            "snapshot",
            "state",
        }
        if not isinstance(body, dict) or set(body) != required:
            raise StoreIntegrityError("checkpoint fields differ")
        if (
            body.get("schema_version") != 1
            or body.get("round_index") != round_index
            or body.get("manifest_sha256") != digest_value(self._manifest)
        ):
            raise StoreIntegrityError("checkpoint identity differs")
        snapshot = body.get("snapshot")
        if (
            not isinstance(snapshot, dict)
            or set(snapshot) != {"sha256", "path", "file_count", "byte_count"}
        ):
            raise StoreIntegrityError("checkpoint snapshot is invalid")
        digest = snapshot.get("sha256")
        if (
            not isinstance(digest, str)
            or snapshot.get("path")
            != self._snapshot_path(digest).relative_to(self.root).as_posix()
            or type(snapshot.get("file_count")) is not int
            or snapshot["file_count"] < 0
            or type(snapshot.get("byte_count")) is not int
            or snapshot["byte_count"] < 0
        ):
            raise StoreIntegrityError("checkpoint snapshot hash is invalid")
        self._verify_snapshot(digest)
        metadata_path = _assert_safe_under(
            self._snapshot_path(digest),
            self._snapshot_path(digest) / "snapshot.json",
            allow_missing=False,
        )
        try:
            metadata = json.loads(_read_bytes_nofollow(metadata_path).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StoreIntegrityError("checkpoint snapshot metadata is invalid") from exc
        if (
            not isinstance(metadata, dict)
            or metadata.get("file_count") != snapshot["file_count"]
            or metadata.get("byte_count") != snapshot["byte_count"]
        ):
            raise StoreIntegrityError("checkpoint snapshot counts differ")
        return {**body, "checkpoint_sha256": checksum, "checkpoint_path": str(checkpoint)}

    def load_checkpoint(self, round_index: int) -> dict[str, Any]:
        with self._lock():
            return self._load_checkpoint_unlocked(round_index)

    def load_final_checkpoint(self) -> dict[str, Any]:
        with self._lock():
            checkpoint = self._path("checkpoints", "final.json", allow_missing=False)
            sidecar = self._path("checkpoints", "final.sha256", allow_missing=False)
            payload = _read_bytes_nofollow(checkpoint)
            try:
                expected = _read_bytes_nofollow(sidecar).decode("ascii").strip()
            except UnicodeDecodeError as exc:
                raise StoreIntegrityError("final checkpoint sidecar is invalid") from exc
            checksum = hashlib.sha256(payload).hexdigest()
            if checksum != expected:
                raise StoreIntegrityError("final checkpoint file hash drifted")
            try:
                body = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StoreIntegrityError("final checkpoint JSON is invalid") from exc
            required = {
                "schema_version",
                "kind",
                "manifest_sha256",
                "journal_head_sha256",
                "journal_events",
                "snapshot",
                "state",
            }
            if (
                not isinstance(body, dict)
                or set(body) != required
                or body.get("schema_version") != 1
                or body.get("kind") != "final"
                or body.get("manifest_sha256") != digest_value(self._manifest)
            ):
                raise StoreIntegrityError("final checkpoint fields differ")
            snapshot = body.get("snapshot")
            if (
                not isinstance(snapshot, dict)
                or set(snapshot) != {"sha256", "path", "file_count", "byte_count"}
                or not isinstance(snapshot.get("sha256"), str)
                or snapshot.get("path")
                != self._snapshot_path(snapshot["sha256"]).relative_to(self.root).as_posix()
                or type(snapshot.get("file_count")) is not int
                or snapshot["file_count"] < 0
                or type(snapshot.get("byte_count")) is not int
                or snapshot["byte_count"] < 0
            ):
                raise StoreIntegrityError("final checkpoint snapshot is invalid")
            state = body.get("state")
            if not isinstance(state, dict):
                raise StoreIntegrityError("final checkpoint state is invalid")
            self._verify_snapshot(snapshot["sha256"])
            metadata_path = _assert_safe_under(
                self._snapshot_path(snapshot["sha256"]),
                self._snapshot_path(snapshot["sha256"]) / "snapshot.json",
                allow_missing=False,
            )
            try:
                metadata = json.loads(_read_bytes_nofollow(metadata_path).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise StoreIntegrityError("final checkpoint snapshot metadata is invalid") from exc
            if (
                not isinstance(metadata, dict)
                or metadata.get("file_count") != snapshot["file_count"]
                or metadata.get("byte_count") != snapshot["byte_count"]
            ):
                raise StoreIntegrityError("final checkpoint snapshot counts differ")
            return {**body, "checkpoint_sha256": checksum, "checkpoint_path": str(checkpoint)}

    def restore_checkpoint(self, round_index: int) -> None:
        with self._lock():
            checkpoint = self._load_checkpoint_unlocked(round_index)
            digest = checkpoint["snapshot"]["sha256"]
            tree = self._verify_snapshot(digest)
            desired_entries, _desired_digest, _bytes = self._inventory_free_path(
                tree,
                ignore_path=None,
            )
            desired = {entry.path: entry for entry in desired_entries}
            current_entries, _digest, _bytes = self._workspace_inventory()
            current = {entry.path: entry for entry in current_entries}
            for relative in sorted(set(current) - set(desired)):
                target = _assert_safe_under(
                    self.workspace,
                    self.workspace / relative,
                    allow_missing=False,
                )
                target.unlink()
            for relative in sorted(desired):
                source = _assert_safe_under(
                    tree,
                    tree / relative,
                    allow_missing=False,
                )
                destination = _assert_safe_under(
                    self.workspace,
                    self.workspace / relative,
                    allow_missing=True,
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                _atomic_write(
                    destination,
                    _read_bytes_nofollow(source),
                    mode=desired[relative].mode,
                )
            _prune_empty_directories(self.workspace, root=self.root)
            _entries, actual, _bytes = self._workspace_inventory()
            if actual != digest:
                raise StoreIntegrityError("restored workspace hash differs from checkpoint")

    def _inventory_free_path(
        self,
        path: Path,
        *,
        ignore_path: Path | None,
    ) -> tuple[tuple[SnapshotEntry, ...], str, int]:
        max_bytes = int(self._manifest["limits"]["snapshot_max_bytes"])
        entries: list[SnapshotEntry] = []
        total = 0
        for current, dirs, files in os.walk(path, topdown=True, followlinks=False):
            current_path = Path(current)
            for name in dirs:
                if _is_link(current_path / name):
                    raise StoreIntegrityError("snapshot tree contains a link")
            for name in sorted(files):
                file_path = current_path / name
                sha, size, mode = _file_sha_nofollow(file_path, max_bytes=max_bytes)
                total += size
                entries.append(
                    SnapshotEntry(
                        path=file_path.relative_to(path).as_posix(),
                        sha256=sha,
                        size=size,
                        mode=mode,
                    )
                )
        entries.sort(key=lambda item: item.path)
        return tuple(entries), digest_value([item.to_dict() for item in entries]), total

    def _inspect_integrity_unlocked(self) -> dict[str, Any]:
        self.task_text()
        events = self._events_unlocked()
        try:
            state = reduce_events(self._manifest, events)
        except StateIntegrityError as exc:
            raise StoreIntegrityError(str(exc)) from exc

        session_records: list[
            tuple[int, str, int, Any, Path]
        ] = []
        seen_session_paths: set[Path] = set()
        for index, event in enumerate(events):
            kind = event["kind"]
            payload = event["payload"]
            if kind not in {
                "manager_completed",
                "executor_completed",
                "auditor_completed",
            }:
                continue
            value = payload.get("episode")
            try:
                episode = parse_role_episode(json.dumps(value, ensure_ascii=False))
            except (TypeError, ValueError) as exc:
                raise StoreIntegrityError(
                    f"{kind} at journal event {index} has invalid episode evidence"
                ) from exc
            if not episode.output.strip() or episode.terminal != "end_turn":
                raise StoreIntegrityError(
                    f"{kind} at journal event {index} has incomplete episode evidence"
                )
            if not episode.cleanup_ok:
                raise StoreIntegrityError(
                    f"{kind} at journal event {index} has failed cleanup evidence"
                )
            raw_path = Path(episode.session_path).expanduser()
            candidate = raw_path if raw_path.is_absolute() else self.root / raw_path
            normalized = candidate.resolve(strict=False)
            if normalized in seen_session_paths:
                raise StoreIntegrityError("recorded session paths must be unique")
            seen_session_paths.add(normalized)
            role = kind.removesuffix("_completed")
            session_records.append(
                (
                    index,
                    role,
                    int(payload["round"]),
                    episode,
                    normalized,
                )
            )

        sessions: list[dict[str, Any]] = []
        for index, role, round_index, episode, _normalized in session_records:
            try:
                session_path = self.validate_session_path(
                    episode.session_path,
                    expected_role=role,
                    expected_round=round_index,
                )
            except StoreIntegrityError as exc:
                raise StoreIntegrityError(
                    f"{role}_completed at journal event {index} "
                    f"session evidence is invalid: {exc}"
                ) from exc
            if role == "auditor":
                report = events[index]["payload"]["report"]
                if isinstance(report, dict) and report.get("status") == "complete":
                    try:
                        self.validate_auditor_tool_evidence(episode.session_path)
                    except StoreIntegrityError as exc:
                        raise StoreIntegrityError(
                            f"complete audit at journal event {index}: {exc}"
                        ) from exc
            sessions.append(
                {
                    "journal_event": index,
                    "kind": f"{role}_completed",
                    "session_path": str(session_path),
                }
            )

        completed_positions = {
            event["payload"]["round"]: index
            for index, event in enumerate(events)
            if event["kind"] == "round_completed"
        }
        if tuple(sorted(completed_positions)) != state.completed_rounds:
            raise StoreIntegrityError("completed round events differ from reducer state")

        checkpoints: list[dict[str, Any]] = []
        latest_snapshot: str | None = None
        for round_index in state.completed_rounds:
            journal_events = completed_positions[round_index] + 1
            checkpoint = self._load_checkpoint_unlocked(round_index)
            if checkpoint["journal_events"] != journal_events:
                raise StoreIntegrityError(
                    f"round {round_index} checkpoint is not bound to its journal prefix"
                )
            if checkpoint["journal_head_sha256"] != events[journal_events - 1]["sha256"]:
                raise StoreIntegrityError(f"round {round_index} checkpoint journal hash drifted")
            try:
                prefix_state = reduce_events(self._manifest, events[:journal_events])
            except StateIntegrityError as exc:
                raise StoreIntegrityError(str(exc)) from exc
            if canonical_json(checkpoint["state"]) != canonical_json(asdict(prefix_state)):
                raise StoreIntegrityError(
                    f"round {round_index} checkpoint state differs from its journal prefix"
                )
            latest_snapshot = checkpoint["snapshot"]["sha256"]
            checkpoints.append(
                {
                    "round": round_index,
                    "checkpoint_sha256": checkpoint["checkpoint_sha256"],
                    "snapshot_sha256": latest_snapshot,
                }
            )

        workspace_digest: str | None
        try:
            _entries, workspace_digest, _bytes = self._workspace_inventory()
        except StoreIntegrityError:
            if state.status != "blocked":
                raise
            workspace_digest = None
        if state.status != "blocked" and workspace_digest is not None:
            if (
                latest_snapshot is None
                and workspace_digest
                != self._manifest["initial_workspace_sha256"]
            ):
                raise StoreIntegrityError(
                    "workspace drifted from the initial workspace digest"
                )
            if latest_snapshot is not None and workspace_digest != latest_snapshot:
                raise StoreIntegrityError("workspace drifted from the latest checkpoint")

        final_checkpoint: dict[str, Any] | None = None
        if state.status == "complete":
            if not (
                self._path("checkpoints", "final.json").is_file()
                and self._path("checkpoints", "final.sha256").is_file()
            ):
                raise StoreIntegrityError("complete run is missing its final checkpoint")
            final = self.load_final_checkpoint()
            if (
                final["journal_events"] != len(events)
                or final["journal_head_sha256"] != events[-1]["sha256"]
                or canonical_json(final["state"]) != canonical_json(asdict(state))
                or final["snapshot"]["sha256"] != workspace_digest
            ):
                raise StoreIntegrityError("final checkpoint differs from complete run state")
            final_checkpoint = {
                "checkpoint_sha256": final["checkpoint_sha256"],
                "snapshot_sha256": final["snapshot"]["sha256"],
            }

        return {
            "state": json.loads(canonical_json(asdict(state)).decode("utf-8")),
            "completed_rounds": list(state.completed_rounds),
            "journal_events": len(events),
            "journal_head_sha256": events[-1]["sha256"],
            "workspace_digest": workspace_digest,
            "sessions": sessions,
            "checkpoints": checkpoints,
            "final_checkpoint": final_checkpoint,
        }

    def inspect_integrity(self) -> dict[str, Any]:
        """Validate all persistent evidence without repairing or writing files.

        This is the public authority for complete status: a directly reduced
        ``complete`` state is insufficient until the final checkpoint and all
        round/session evidence validate here.
        """

        with self._lock():
            return self._inspect_integrity_unlocked()

    def resume_state(self) -> RunState:
        with self._lock():
            summary = self._inspect_integrity_unlocked()
            events = self._events_unlocked()
            try:
                state = reduce_events(self._manifest, events)
            except StateIntegrityError as exc:
                raise StoreIntegrityError(str(exc)) from exc
            if canonical_json(summary["state"]) != canonical_json(asdict(state)):
                raise StoreIntegrityError("integrity summary differs from reducer output")
            try:
                assert_resume_allowed(state)
            except ResumeBlockedError as exc:
                raise StoreIntegrityError(str(exc)) from exc
            return state


def _prune_empty_directories(workspace: Path, *, root: Path) -> None:
    for current, dirs, files in os.walk(workspace, topdown=False, followlinks=False):
        current_path = Path(current)
        if current_path == workspace:
            continue
        if current_path.name in _IGNORED_COMPONENTS:
            continue
        resolved = current_path.resolve(strict=False)
        if resolved == root or _inside(resolved, root):
            continue
        if not dirs and not files:
            try:
                current_path.rmdir()
            except OSError:
                pass
