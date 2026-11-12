"""Stable artifact references + bounded read-back for large tool outputs.

Ownership: one store per resolved workspace; artifacts live under
``<workspace>/.my_agent_core/artifacts/<owner>/<sha256>.txt`` with a JSON sidecar.
Content addressing means a different body can never overwrite an old artifact,
even when the same ``tool_call_id`` is reused.

Full text is persisted first; only then a handle is returned. The model-visible
transcript keeps a bounded head+tail preview, while Session keeps the original
tool message. Re-preparing the same result yields the same artifact id and the
same preview bytes.

Read-back is byte-paged (Unicode-safe) and search is capped; both are trusted
tools and MUST NOT be re-externalized into new artifacts.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024
READ_MAX_BYTES = 48 * 1024
READ_MAX_BYTES_HARD = 96 * 1024
SEARCH_MAX_RESULTS = 20
SEARCH_MAX_LINE_CHARS = 200
SEARCH_MAX_SCAN_BYTES = 4 * 1024 * 1024
PREVIEW_HEAD_CHARS = 3500
PREVIEW_TAIL_CHARS = 1500
PREVIEW_THRESHOLD = 6000

# Tools whose own output is already bounded; never wrap them again (no recursion).
TRUSTED_BOUNDED_TOOLS = frozenset({"artifact_read", "artifact_search"})
_TRUNCATION_MARKERS = (
    "[Output truncated",
    "lines truncated",
    "[truncated",
    "... (truncated",
    "output truncated",
)


def looks_truncated(text: str) -> str | None:
    """Return the marker that proves the source was already truncated, if any."""
    window = ((text or "")[:4000] + "\n" + (text or "")[-2000:]).lower()
    for marker in _TRUNCATION_MARKERS:
        if marker.lower() in window:
            return marker
    return None


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    owner: str
    path: Path
    size: int
    sha256: str

    def handle(self) -> str:
        return f"artifact:{self.owner}:{self.artifact_id}"


class ArtifactError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ArtifactStore:
    """Workspace-owned, content-addressed artifact store."""

    def __init__(self, workspace: str | Path, root: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.owner = hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()[:16]
        self.root = Path(root) if root is not None else self.workspace / ".my_agent_core" / "artifacts"
        self._owner_dir = self.root / self.owner

    # ── 保存 ────────────────────────────────────────────────
    def save(self, text: str, *, tool_name: str, tool_call_id: str) -> ArtifactRef:
        data = (text or "").encode("utf-8")
        if not data:
            raise ArtifactError("empty", "nothing to save")
        if len(data) > MAX_ARTIFACT_BYTES:
            raise ArtifactError("too_large", f"artifact {len(data)} bytes exceeds cap")
        if text.startswith(("[ARTIFACT artifact:", "[ARTIFACT UNAVAILABLE:")):
            raise ArtifactError("source_projected", "a projected artifact preview is not original output")
        marker = looks_truncated(text)
        if marker:
            raise ArtifactError("source_truncated", f"source tool result already truncated ({marker})")
        digest = hashlib.sha256(data).hexdigest()
        artifact_id = digest[:32]
        try:
            self._checked_path(self._owner_dir)
            self._owner_dir.mkdir(parents=True, exist_ok=True)
            path = self._checked_path(self._owner_dir / f"{artifact_id}.txt")
            sidecar = self._checked_path(self._owner_dir / f"{artifact_id}.json")
            if path.exists():
                if path.read_bytes() != data:
                    raise ArtifactError("corrupt", "existing artifact content mismatch")
            else:
                # Exclusive creation never replaces an old content-addressed object.
                with path.open("xb") as stream:
                    stream.write(data)
            if not sidecar.exists():
                meta = {
                    "artifact_id": artifact_id,
                    "owner": self.owner,
                    "sha256": digest,
                    "bytes": len(data),
                    "tool_name": tool_name,
                    "tool_call_id": tool_call_id,
                    "workspace": str(self.workspace),
                    "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
                with sidecar.open("x", encoding="utf-8") as stream:
                    json.dump(meta, stream, ensure_ascii=False)
        except OSError as exc:
            raise ArtifactError("save_failed", "artifact could not be persisted") from exc
        return ArtifactRef(artifact_id=artifact_id, owner=self.owner, path=path, size=len(data), sha256=digest)

    # ── 解析 ────────────────────────────────────────────────
    def resolve(self, ref: str) -> ArtifactRef:
        """Accept ``artifact:<owner>:<id>`` or a bare id. Reject other owners/paths."""
        raw = (ref or "").strip()
        owner = self.owner
        artifact_id = raw
        if raw.startswith("artifact:"):
            parts = raw.split(":")
            if len(parts) != 3:
                raise ArtifactError("bad_ref", "expected artifact:<owner>:<id>")
            owner, artifact_id = parts[1], parts[2]
        if owner != self.owner:
            raise ArtifactError("foreign_owner", "artifact belongs to another workspace")
        if not re.fullmatch(r"[0-9a-f]{32}", artifact_id or ""):
            raise ArtifactError("bad_ref", "invalid artifact id")
        path = self._checked_path(self._owner_dir / f"{artifact_id}.txt")
        try:
            if not path.is_file():
                raise ArtifactError("missing", "artifact not found or cleaned up")
            if path.stat().st_size > MAX_ARTIFACT_BYTES:
                raise ArtifactError("too_large", "artifact exceeds storage cap")
            data = path.read_bytes()
        except OSError as exc:
            raise ArtifactError("unreadable", "artifact is not readable") from exc
        if hashlib.sha256(data).hexdigest()[:32] != artifact_id:
            raise ArtifactError("corrupt", "artifact content hash mismatch")
        return ArtifactRef(
            artifact_id=artifact_id, owner=owner, path=path, size=len(data), sha256=hashlib.sha256(data).hexdigest()
        )

    def _checked_path(self, path: Path) -> Path:
        """Reject links, including redirected storage roots and owner directories."""
        try:
            resolved = path.resolve()
            resolved.relative_to(self.workspace)
            current = path
            while current != self.workspace:
                if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                    raise ArtifactError("escape", "artifact storage contains a link")
                if current.parent == current:
                    raise ArtifactError("escape", "artifact path escaped workspace")
                current = current.parent
        except (ValueError, OSError) as exc:
            raise ArtifactError("escape", "artifact path escaped workspace") from exc
        return path

    # ── 分页读取 ────────────────────────────────────────────
    def read_page(self, ref: str, *, cursor: int = 0, max_bytes: int = READ_MAX_BYTES) -> dict:
        artifact = self.resolve(ref)
        try:
            data = artifact.path.read_bytes()
        except OSError as exc:
            raise ArtifactError("unreadable", "artifact is not readable") from exc
        limit = max(256, min(int(max_bytes or READ_MAX_BYTES), READ_MAX_BYTES_HARD))
        start = int(cursor or 0)
        if start < 0 or start > len(data):
            raise ArtifactError("bad_cursor", "cursor outside artifact")
        if start < len(data) and data[start] & 0xC0 == 0x80:
            raise ArtifactError("bad_cursor", "cursor must be a UTF-8 character boundary")
        end = min(len(data), start + limit)
        chunk = data[start:end]
        # Unicode safety: never cut a multi-byte char; shrink until it decodes.
        while chunk:
            try:
                text = chunk.decode("utf-8")
                break
            except UnicodeDecodeError:
                chunk = chunk[:-1]
                end -= 1
        else:
            text = ""
        next_cursor = end
        return {
            "artifact_id": artifact.artifact_id,
            "text": text,
            "cursor": next_cursor,
            "start_cursor": start,
            "next_cursor": next_cursor if end < len(data) else None,
            "truncated": end < len(data),
            "bytes_total": len(data),
        }

    # ── 搜索 ────────────────────────────────────────────────
    def search(self, ref: str, query: str, *, max_results: int = SEARCH_MAX_RESULTS) -> dict:
        artifact = self.resolve(ref)
        needle = (query or "").strip()
        if len(needle) > 200:
            raise ArtifactError("bad_query", "query exceeds 200 characters")
        if not needle:
            raise ArtifactError("bad_query", "empty query")
        try:
            with artifact.path.open("rb") as stream:
                raw = stream.read(SEARCH_MAX_SCAN_BYTES)
        except OSError as exc:
            raise ArtifactError("unreadable", "artifact is not readable") from exc
        text = raw.decode("utf-8", errors="ignore")
        hits: list[dict] = []
        capped = max(1, min(int(max_results or SEARCH_MAX_RESULTS), SEARCH_MAX_RESULTS))
        for lineno, line in enumerate(text.splitlines(), 1):
            if needle.lower() in line.lower():
                snippet = line.strip()
                if len(snippet) > SEARCH_MAX_LINE_CHARS:
                    pos = snippet.lower().find(needle.lower())
                    left = max(0, pos - SEARCH_MAX_LINE_CHARS // 2)
                    snippet = snippet[left : left + SEARCH_MAX_LINE_CHARS]
                hits.append({"line": lineno, "text": snippet})
                if len(hits) > capped:
                    break
        return {
            "artifact_id": artifact.artifact_id,
            "query": needle,
            "hits": hits[:capped],
            "truncated": len(hits) > capped or len(raw) < artifact.size,
        }


def render_preview(text: str, store: ArtifactStore, *, tool_name: str, tool_call_id: str) -> tuple[str, dict]:
    """Build the model-visible bounded preview and its metadata.

    Returns ``(preview_text, metadata)``. The full body is saved before a handle is
    advertised. When saving fails, an explicit ``full_unavailable`` reason is shown
    and no readable handle is returned.
    """
    if len(text) <= PREVIEW_THRESHOLD:
        return text, {"artifact_preview": False}
    head = text[:PREVIEW_HEAD_CHARS]
    tail = text[-PREVIEW_TAIL_CHARS:]
    try:
        ref = store.save(text, tool_name=tool_name, tool_call_id=tool_call_id)
    except ArtifactError as exc:
        preview = (
            f"[ARTIFACT UNAVAILABLE: {exc.code}] {exc}\n"
            "[Showing bounded head/tail only; no artifact handle exists.]\n"
            f"--- head ---\n{head}\n--- tail ---\n{tail}"
        )
        return preview, {"artifact_preview": True, "full_unavailable": exc.code, "artifact_id": None}
    preview = (
        f"[ARTIFACT {ref.handle()} bytes={ref.size}]\n"
        f"--- head ---\n{head}\n--- tail ---\n{tail}\n"
        f"[Use artifact_read with id={ref.handle()} and cursor=0 for the full text.]"
    )
    return preview, {"artifact_preview": True, "artifact_id": ref.handle(), "artifact_bytes": ref.size}


def apply_previews(messages: list, store: ArtifactStore | None) -> tuple[list, bool]:
    """Return a view with large tool messages replaced by stable previews.

    Messages are copied, never mutated: Session keeps the original full text.
    Trusted bounded tools (artifact_read/search) are never wrapped again.
    """
    if store is None:
        return messages, False
    out = []
    changed = False
    for message in messages:
        meta = message.metadata if isinstance(message.metadata, dict) else {}
        bounded = meta.get("artifact_bounded") is True and meta.get("tool_name") in TRUSTED_BOUNDED_TOOLS
        if message.role == "tool" and not bounded and not meta.get("artifact_preview"):
            content = message.content if isinstance(message.content, str) else None
            if content and len(content) > PREVIEW_THRESHOLD:
                preview, pmeta = render_preview(
                    content,
                    store,
                    tool_name=str(meta.get("tool_name") or "tool"),
                    tool_call_id=str(meta.get("tool_call_id") or ""),
                )
                if pmeta.get("artifact_preview"):
                    new_meta = dict(meta)
                    new_meta.update(pmeta)
                    out.append(message.model_copy(update={"content": preview, "metadata": new_meta}))
                    changed = True
                    continue
        out.append(message)
    return out, changed
