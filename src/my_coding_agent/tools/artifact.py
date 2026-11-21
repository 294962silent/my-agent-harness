"""Read-only artifact tools bound to one workspace-owned store."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from my_agent_core.artifacts import (
    MAX_ARTIFACT_BYTES,
    READ_MAX_BYTES,
    SEARCH_MAX_RESULTS,
    ArtifactError,
    ArtifactStore,
)
from my_agent_core.tools import Tool, ToolResult, tool


class BoundedArtifactResult(ToolResult):
    """The factory enforces output caps; generic line truncation loses page bytes."""

    def serialize(self) -> str:
        return str(self.data) if self.ok else str(self.error)


def make_artifact_tools(store: ArtifactStore | None = None, workspace: str | Path | None = None) -> list[Tool]:
    """Build the two always-on, bounded, read-only artifact tools."""
    store = store or ArtifactStore(workspace or Path.cwd())

    @tool(
        name="artifact_read",
        description=(
            "Read a saved large tool output by artifact handle, in bounded byte pages. "
            "Pass the next_cursor from the previous page to continue; Unicode characters "
            "are never split. Trusted bounded tool: its output is never re-externalized."
        ),
        prompt_snippet="Read a saved artifact page",
        is_parallel_safe=True,
    )
    async def artifact_read(id: str, cursor: int = 0, max_bytes: int = READ_MAX_BYTES) -> Any:
        try:
            page = store.read_page(id, cursor=cursor, max_bytes=max_bytes)
        except ArtifactError as exc:
            return BoundedArtifactResult(ok=False, error=f"Error: {exc.code}: {exc}")
        tail = f"\n[next_cursor={page['next_cursor']}]" if page["next_cursor"] is not None else "\n[end_of_artifact]"
        return BoundedArtifactResult(
            ok=True,
            data=(
                f"[artifact {page['artifact_id']} bytes {page['start_cursor']}-{page['cursor']}"
                f" of {page['bytes_total']}]\n{page['text']}{tail}"
            ),
        )

    @tool(
        name="artifact_search",
        description=(
            "Search inside a saved artifact with bounded snippets (max 20 hits). "
            "Returns line numbers and explicit truncated state."
        ),
        prompt_snippet="Search a saved artifact",
        is_parallel_safe=True,
    )
    async def artifact_search(id: str, query: str, max_results: int = SEARCH_MAX_RESULTS) -> Any:
        try:
            found = store.search(id, query, max_results=max_results)
        except ArtifactError as exc:
            return BoundedArtifactResult(ok=False, error=f"Error: {exc.code}: {exc}")
        lines = [
            f"artifact {found['artifact_id']} query={found['query']!r} hits={len(found['hits'])} truncated={found['truncated']}"
        ]
        lines += [f"{h['line']}: {h['text']}" for h in found["hits"]]
        if not found["hits"]:
            lines.append("(no matches)")
        return BoundedArtifactResult(ok=True, data="\n".join(lines))

    for bounded_tool in (artifact_read, artifact_search):
        bounded_tool._artifact_bounded = True
    return [artifact_read, artifact_search]


__all__ = ["make_artifact_tools", "MAX_ARTIFACT_BYTES"]
