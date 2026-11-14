"""Optional candidate index contract; local memory remains authoritative."""
from __future__ import annotations

from typing import Protocol, TypedDict


class MemoryCandidate(TypedDict):
    scope: str
    identity: str
    revision: str


class MemoryRecord(MemoryCandidate):
    text: str


class SemanticMemoryIndex(Protocol):
    async def search(self, *, scope: str, query: str, records: list[MemoryRecord]) -> list[MemoryCandidate]: ...

    async def aclose(self, timeout: float = 0.1) -> bool: ...
