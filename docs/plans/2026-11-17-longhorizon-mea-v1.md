# Long-horizon MEA v1 implementation plan

Date: 2026-11-17

## Objective

Add a minimal, runnable CLI coding-task layer that borrows the verified
Manager -> Executor -> Auditor loop and checkpoint/recovery discipline from
LongHorizon-Harness without replacing the existing ReAct, Session, compaction,
tool, artifact or provider infrastructure.

The first version is intentionally CLI-only. GUI control, web dashboards,
multi-backend adapter parity and autonomous policy evolution are out of scope.
RSI experiments remain paused.

## Stable task contract

The user's task text is the stable contract. The Manager may decompose it into
bounded subtasks, but it cannot silently replace it with a simpler objective.
Optional acceptance commands are stored in the manifest and shown to the
Auditor.

## Role boundaries

- Manager: no tools. Receives the original task, verified facts, prior audit
  summaries, queued follow-up input and round budget. Returns one JSON decision:
  `execute`, `done`, `blocked` or `ask`.
- Executor: a fresh `CodingAgent` and fresh Session per round. Receives only the
  assigned subtask, acceptance criteria, verified facts and cited evidence.
- Auditor: a fresh `CodingAgent` and fresh Session per round. Uses a
  `PermissionGate(mode="review")`: read tools and explicitly safe commands are
  allowed; write/edit and arbitrary shell commands fail closed. Independently
  inspects the workspace and returns a strict JSON report.

## Persistent state

Each run uses an immutable manifest and hash-chained append-only journal.

```text
prepared
round_started
manager_completed
executor_completed
auditor_completed
round_completed
run_completed
round_unknown
```

`round_started` is written and fsynced before any model call. Any dangling
started round makes `resume` fail closed. A resume proceeds only from a
contiguous prefix of completed rounds and only after the current workspace hash
matches the latest checkpoint.

An Auditor mutation of the workspace during its read-only episode invalidates
the audit. The run becomes blocked rather than silently trusting the result.

## Checkpoints

Every completed round records:

- role outputs and Session paths;
- model usage when known;
- a bounded content-addressed workspace snapshot;
- the latest verified-state reducer output;
- a SHA-256 of the checkpoint file.

`restore` is explicit and operator-invoked. Automatic resume never rewrites the
workspace.

## CLI

```text
lh-longhorizon run --task @task.md --workspace . --provider openai \
  --model MODEL --auth-profile default --max-rounds 8
lh-longhorizon status RUN_ID
lh-longhorizon resume RUN_ID
lh-longhorizon follow-up RUN_ID --message @followup.md
lh-longhorizon restore RUN_ID --round N
```

`status` is read-only. Credentials are resolved only immediately before a
model-backed run, are never written to the manifest or journal, and provider
retries default to zero.

## File layout

- `src/my_coding_agent/longhorizon/__init__.py`
- `src/my_coding_agent/longhorizon/schema.py`
- `src/my_coding_agent/longhorizon/store.py`
- `src/my_coding_agent/longhorizon/state.py`
- `src/my_coding_agent/longhorizon/roles.py`
- `src/my_coding_agent/longhorizon/engine.py`
- `src/my_coding_agent/longhorizon/cli.py`
- `tests/longhorizon/`

## Shared interfaces

The package exposes these names. Implementers must use them verbatim so the
store/state and role/controller work can be integrated without adapter glue.

```python
@dataclass(frozen=True)
class ManagerDecision:
    next: Literal["execute", "done", "blocked", "ask"]
    reason: str
    subtask: str = ""
    acceptance_criteria: tuple[str, ...] = ()
    question: str = ""
    related_rounds: tuple[int, ...] = ()

@dataclass(frozen=True)
class AuditReport:
    status: Literal["complete", "incomplete", "blocked"]
    summary: str
    evidence: tuple[str, ...]
    verified_facts: tuple[str, ...] = ()
    remaining: tuple[str, ...] = ()

@dataclass(frozen=True)
class RoleEpisode:
    output: str
    terminal: str
    iterations: int
    tool_names: tuple[str, ...]
    usage: tuple[dict[str, int], ...]
    session_path: str
    cleanup_ok: bool

class RoleBackend(Protocol):
    async def manager(
        self, *, task: str, state: Mapping[str, Any], round_index: int
    ) -> RoleEpisode: ...

    async def executor(
        self, *, task: str, decision: ManagerDecision,
        state: Mapping[str, Any], round_index: int,
    ) -> RoleEpisode: ...

    async def auditor(
        self, *, task: str, decision: ManagerDecision,
        executor_output: str, state: Mapping[str, Any], round_index: int,
    ) -> RoleEpisode: ...

def parse_manager_decision(text: str) -> ManagerDecision: ...
def parse_audit_report(text: str) -> AuditReport: ...

class LongHorizonStore:
    @classmethod
    def prepare(cls, root: Path, *, run_id: str, task: str,
                workspace: Path, model: Mapping[str, Any],
                limits: Mapping[str, Any]) -> "LongHorizonStore": ...
    @classmethod
    def load(cls, root: Path) -> "LongHorizonStore": ...
    @property
    def manifest(self) -> dict[str, Any]: ...
    def events(self) -> list[dict[str, Any]]: ...
    def append(self, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]: ...
    def capture_workspace(self) -> WorkspaceSnapshot: ...
    def write_checkpoint(self, round_index: int, state: Mapping[str, Any],
                         snapshot: WorkspaceSnapshot) -> dict[str, Any]: ...
    def load_checkpoint(self, round_index: int) -> dict[str, Any]: ...
    def restore_checkpoint(self, round_index: int) -> None: ...
    def set_status(self, value: Mapping[str, Any]) -> None: ...

@dataclass(frozen=True)
class RunState:
    status: Literal["ready", "waiting_input", "blocked", "complete"]
    next_round: int
    completed_rounds: tuple[int, ...]
    verified_facts: tuple[str, ...]
    last_audit: dict[str, Any] | None
    last_manager: dict[str, Any] | None
    active_round: int | None
    terminal_reason: str

def reduce_events(manifest: Mapping[str, Any],
                  events: Sequence[Mapping[str, Any]]) -> RunState: ...
```

`LongHorizonStore.append` owns the fsynced hash chain. `RunState` is derived
only from the manifest and validated journal; callers never get to edit it in
place.

## Implementation tasks

### Task 1: Schema and JSON contracts

Create strict dataclasses/validators for `ManagerDecision`, `AuditReport`,
`RoleResult`, `RoundRecord` and reduced run state. Parser accepts a fenced JSON
object but rejects extra fields, missing required fields and invalid enums.

Tests cover valid decisions, fenced JSON, malformed JSON, unknown routes,
missing evidence and forbidden extra fields.

### Task 2: Journal, checkpoints and reducer

Implement canonical JSON, atomic writes, run manifest creation, hash-chain
journal validation, bounded workspace snapshots, checkpoint persistence and a
pure reducer. Recovery must reject hash drift, duplicate sequence, torn journal,
non-contiguous rounds, dangling starts, workspace drift and completed-run
resume.

Tests cover every rejection path plus explicit checkpoint restore.

### Task 3: Role backend

Implement production role execution with one shared `LLM` and fresh
`CodingAgent`/Session instances for Executor and Auditor. Manager uses direct
`LLM.achat`. Capture visible output, terminal stop reason, tool names and known
usage. Auditor uses review-mode PermissionGate and detects workspace mutation.

Tests use fake LLM/agent seams; no real provider calls.

### Task 4: Controller and CLI

Implement `LongHorizonRun.run_round`, `run_until_terminal`, `status`, `resume`,
durable follow-up input and explicit restore. Add `python -m
my_coding_agent.longhorizon.cli` and the `lh-longhorizon` console entry.

Tests use a scripted backend to prove:

- `round_started` precedes role calls;
- clean audit is required before `done`;
- incomplete audit continues to the next Manager round;
- dangling execution blocks resume;
- follow-up input reaches the next Manager prompt;
- status never loads credentials or mutates the run.

### Task 5: Integration validation

Run the new tests, the existing Session/Agent/research integration tests and
Ruff. Create a temporary coding workspace and run a scripted end-to-end task:
Manager asks for a fix, fake Executor edits a file, read-only Auditor verifies
it, Manager completes, a second process reads the committed checkpoint.

Only after offline validation should a small real-provider smoke be attempted.
Rsi-related work is not part of this validation.

## v1.1 recovery and protected-input extension

- `--pause-after-round N` stops only after round N is fully audited and
  checkpointed; a new process can resume at round N+1.
- `resume --pause-after-round N` validates `N` against the last completed
  round immediately after the store integrity read and before credential, LLM,
  backend, or controller construction. A stale or non-positive pause value is
  rejected without loading auth.
- `--immutable-path PATH` accepts a workspace-relative path or an absolute path
  lexically inside the workspace. Absolute paths are converted to relative
  paths, and the manifest stores only workspace-relative strings.
- The CLI rejects drive-relative paths and any `..` component before calling the
  store. It does not resolve or canonicalize candidate paths, so internal
  symlink or junction components are preserved for the store to reject.
- The store treats immutable-path input as untrusted and rejects drive-relative
  paths, `..`, symlinks, and junctions fail-closed. Executor changes to a
  protected path block the round before audit.
- Protected-path existence and hashes are compared against the pre-Executor
  snapshot; the initial workspace digest remains frozen in the manifest.
- Real validation must use separate `run` and `resume` processes.
