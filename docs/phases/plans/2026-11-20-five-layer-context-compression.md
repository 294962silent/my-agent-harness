# Five-Layer Context Compression Implementation Plan

> **For agentic workers:** execute the tasks in order. Each task must start with
> a failing test, then the minimal implementation, then the listed verification.

**Goal:** Implement five deterministic, observable, request-view compression
layers without deleting canonical Session history.

**Architecture:** A new pure projection module owns message identities and the
five layers. `ContextManager` owns budget measurement, epoch freezing, model
calls, and diagnostics. Session persistence remains append-only and
backward-compatible.

**Tech Stack:** Python 3.11/3.13, Pydantic message models, pytest, JSONL Session,
existing `ArtifactStore`.

## Global Constraints

- Claude Code internals are unavailable; match the approved observable behavior.
- Never delete canonical Session messages.
- Layer order is Tool Result Budget, Snip, Microcompact, Context Collapse,
  Auto-Compact.
- Microcompact requires explicit cache invalidation.
- Repeated projection of unchanged history must be byte-identical.
- No real model or benchmark run is required for unit acceptance.
- Preserve all unrelated dirty-worktree files.

## Phase Index

- `PHASE-02.md`: configuration, identities, Layer 1
- `PHASE-03.md`: Snip and projection-plan persistence
- `PHASE-04.md`: Microcompact and cache invalidation
- `PHASE-05.md`: Context Collapse
- `PHASE-06.md`: Auto-Compact integration, docs, full regression

---

### Task 1: Configuration and stable message identities

**Files:**
- Create: `src/my_agent_core/context_compression.py`
- Modify: `src/my_agent_core/context.py`
- Modify: `src/my_agent_core/events.py`
- Modify: `src/my_agent_core/types.py`
- Modify: `src/my_agent_core/agent.py`
- Modify: `src/my_coding_agent/settings.py`
- Modify: `src/my_coding_agent/agent.py`
- Modify: `src/my_coding_agent/rpc_server.py`
- Modify: `src/my_coding_agent/rpc/session_handlers.py`
- Test: `tests/core/test_context_compression.py`

**Interfaces:**

```python
@dataclass(frozen=True)
class CompressionConfig:
    enabled: bool = True
    reserve_tokens: int = 16_384
    keep_recent_tokens: int = 20_000
    tool_result_max_chars: int = 6_000
    tool_round_max_chars: int = 24_000
    context_trigger_ratio: float = 0.8
    microcompact_min_chars: int = 200

@dataclass(frozen=True)
class ProjectionItem:
    projection_id: str
    message: Message
    original_index: int

def project_messages(messages: list[Message]) -> list[ProjectionItem]: ...
```

- [ ] Write tests for duplicate-safe IDs and invalid config rejection.
- [ ] Run tests and verify they fail because the module does not exist.
- [ ] Implement validation and stable IDs using role, canonical content,
  metadata digest, and duplicate ordinal.
- [ ] Wire `CompactionSettings` into every RPC `CodingAgent` construction.
- [ ] Run targeted tests and Ruff.

### Task 2: Tool Result Budget

**Files:**
- Modify: `src/my_agent_core/context_compression.py`
- Modify: `src/my_agent_core/context.py`
- Modify: `src/my_agent_core/artifacts.py`
- Test: `tests/core/test_context_compression.py`
- Test: `tests/core/test_artifacts.py`

**Interfaces:**

```python
def apply_tool_result_budget(
    messages: list[Message],
    *,
    artifact_store: ArtifactStore | None,
    results_dir: Path | None,
    config: CompressionConfig,
) -> tuple[list[Message], dict[str, Any]]: ...
```

- [ ] Write a failing test showing a large single result is externalized.
- [ ] Write a failing test showing the same-turn aggregate budget externalizes
  largest results deterministically.
- [ ] Verify preview head/tail, stable artifact IDs, and tool pairing.
- [ ] Implement per-result and per-group budgeting.
- [ ] Preserve the legacy `results_dir` behavior when no artifact store exists.
- [ ] Add request-projection reason `layer1:tool_budget`.
- [ ] Run context and artifact tests.

### Task 3: Snip and projection-plan persistence

**Files:**
- Modify: `src/my_agent_core/context_compression.py`
- Modify: `src/my_agent_core/context.py`
- Modify: `src/my_agent_core/session/entries.py`
- Modify: `src/my_agent_core/session/session.py`
- Modify: `src/my_agent_core/session/memory.py`
- Test: `tests/core/test_context_compression.py`
- Test: `tests/core/test_session.py`
- Test: `tests/core/test_session_memory_and_storage.py`

**Interfaces:**

```python
def select_snip_ids(
    items: list[ProjectionItem],
    *,
    keep_recent_tokens: int,
    ratio: float | None,
) -> list[str]: ...

def apply_snip(items: list[ProjectionItem], ids: set[str]) -> list[Message]: ...
```

- [ ] Write failing tests for complete-group deletion and protected tail.
- [ ] Write failing tests proving Snip never splits call/result pairs.
- [ ] Add projection-only CompactionEntry metadata persistence.
- [ ] Ignore projection-only entries in summary lookup and session folding.
- [ ] Persist and validate source branch, canonical hash, selected IDs, and
  recent-tail key.
- [ ] Add request-projection reason `layer2:snip`.
- [ ] Run targeted context/session tests.

### Task 4: Microcompact and cache invalidation

**Files:**
- Modify: `src/my_agent_core/context_compression.py`
- Modify: `src/my_agent_core/context.py`
- Modify: `src/my_agent_core/agent.py`
- Modify: `src/my_agent_core/loop.py`
- Test: `tests/core/test_context_compression.py`
- Test: `tests/core/test_agent.py`
- Test: `tests/llm/test_request_projection.py`

**Interfaces:**

```python
def apply_microcompact(
    items: list[ProjectionItem],
    *,
    keep_recent_tokens: int,
    min_chars: int,
    ratio: float | None,
) -> tuple[list[Message], list[str]]: ...

def invalidate_cache_epoch(self, reason: str) -> None: ...
```

- [ ] Write failing tests for no-op with a valid cache epoch.
- [ ] Write failing tests for body replacement with preserved tool structure.
- [ ] Wire invalidation for model switch, branch/resume, summary replacement,
  and provider-reported cache miss.
- [ ] Freeze selected tool IDs in the active projection plan.
- [ ] Add request-projection reason `layer3:microcompact`.
- [ ] Run context, Agent, and projection tests.

### Task 5: Context Collapse

**Files:**
- Modify: `src/my_agent_core/context_compression.py`
- Modify: `src/my_agent_core/context.py`
- Modify: `src/my_agent_core/session/session.py`
- Test: `tests/core/test_context_compression.py`
- Test: `tests/core/test_context_boundary.py`

**Interfaces:**

```python
def select_collapse_range(
    items: list[ProjectionItem],
    *,
    keep_recent_tokens: int,
    ratio: float | None,
) -> tuple[int, int] | None: ...

async def _collapse_middle(
    self,
    items: list[ProjectionItem],
    start: int,
    end: int,
) -> tuple[list[Message], dict[str, Any]]: ...
```

- [ ] Write failing tests that protected head and recent tail remain unchanged.
- [ ] Write a failing test that only the selected middle range reaches the
  summarizer.
- [ ] Implement collapse prompt and local summary projection.
- [ ] Persist source IDs and summary; reject stale branch/resume plans.
- [ ] Ensure empty/failed summaries skip to Auto-Compact.
- [ ] Run context boundary tests.

### Task 6: Auto-Compact integration and final verification

**Files:**
- Modify: `src/my_agent_core/context.py`
- Modify: `src/my_agent_core/events.py`
- Modify: `src/my_agent_core/agent.py`
- Modify: `docs/core/05-context-compaction.md`
- Modify: `docs/experiments/HARNESS_EXPERIMENT_LOG.md`
- Test: `tests/core/test_context.py`
- Test: `tests/core/test_context_boundary.py`
- Test: `tests/coding/test_settings.py`

- [ ] Write failing pipeline-order tests for all five layers.
- [ ] Integrate Auto-Compact as the final fallback.
- [ ] Extend `ContextCompacted` with defaulted layer/status metadata.
- [ ] Mark `budget_exceeded` when all layers run out.
- [ ] Verify idempotence and append-only prefix after a frozen plan.
- [ ] Verify `enabled=false`, `reserve_tokens`, and recent-tail settings through
  RPC construction.
- [ ] Run:
  ```powershell
  .\.venv\Scripts\python.exe -m pytest tests/core tests/coding/test_settings.py tests/llm/test_request_projection.py tests/llm/test_openai_provider.py tests/llm/test_anthropic_provider.py -q -o addopts= --import-mode=importlib
  .\.venv\Scripts\ruff.exe check src tests
  git diff --check
  ```
- [ ] Update docs with exact implemented behavior and verified counts.
