# Long-horizon MEA v1

Date: 2026-11-17; v1.1 validation: 2026-11-18

Status: v1 implemented and validated on a real two-round CLI coding task; v1.1
final evidence is based on the current worktree at HEAD
`9b1a7c499f13b014cc6f3d0bd5f677092ce1cac6` plus the uncommitted v1.1 changes,
including cross-process pause/resume, immutable-path protection, protected
snapshot checks, and verified local and Langfuse observations.

## Goal

Add a durable Manager -> Executor -> Auditor execution layer for CLI coding
tasks without replacing the existing ReAct loop, Session, compaction, artifact,
tool, provider or evaluation infrastructure.

The layer must:

- keep the user task as a stable contract;
- give each executor and auditor a fresh context;
- accept progress only from an independent audit;
- persist a hash-chained journal and content-addressed checkpoints;
- refuse automatic replay after an interrupted round;
- support status, resume, follow-up and explicit restore;
- expose Langfuse-compatible traces for real model calls.

## Architecture

The new production package is `src/my_coding_agent/longhorizon/`.

- `schema.py`: strict decisions, audit reports, role episodes and round records.
- `store.py`: immutable manifest, hash-chain journal, workspace snapshots,
  round checkpoints, final checkpoint, integrity inspection and restore.
- `state.py`: pure event reducer and resume gate.
- `roles.py`: no-tool Manager and fresh Executor/Auditor core Agents.
- `engine.py`: MEA controller, round protocol, follow-up and recovery.
- `cli.py`: `run`, `status`, `resume`, `follow-up` and `restore`.

The user's task is the stable contract. The Manager receives only that task,
verified facts, prior audit state and queued operator input. The Executor gets
one bounded subtask and a new Session. The Auditor gets the Executor's claim as
untrusted input and receives a fresh copy of the post-Executor workspace. Its
shell surface is one of four exact pytest commands, without arguments or shell
metacharacters; a strict PermissionGate is a second defense.

## Persistence and recovery

The journal uses `sequence -> previous hash -> event hash`; the manifest hash is
embedded in every event. A round is only considered closed after a
`round_completed` event. Each closed round has a checkpoint bound to its journal
prefix, reducer state and workspace snapshot. A completed run has an additional
independent `final.json` checkpoint.

Resume is allowed only when:

- the journal hash chain is valid;
- completed rounds form a contiguous prefix;
- no `round_started` is dangling;
- all historical Session files exist;
- each role/round uses a unique, nonempty JSONL Session;
- a completed Auditor Session contains a successful allowed tool receipt;
- the current workspace digest matches the latest checkpoint.

An interrupted round is never replayed automatically. `restore` is explicit and
rewrites the workspace from a selected checkpoint.

## Borrowed mechanisms

The implementation follows the MEA concepts from AMAP-ML/LongHorizon-Harness but
uses our own code and storage contracts:

- external task state instead of one growing execution context;
- Manager, fresh-context Executor and read-only Auditor;
- verified evidence rather than Executor self-report;
- durable round records and resume;
- completion grounded in the latest clean audit.

Our stricter differences are:

- strict JSON role contracts rather than heading/regex parsing;
- SHA-256 hash chain and content-addressed checkpoints rather than latest-line
  wins;
- dangling rounds fail closed rather than being replayed;
- Playwright/GUI, web dashboard and multi-backend supervisor parity are out of
  scope;
- Auditor-side workspace mutation is detected and blocks the run.

## Automated validation

```text
uv run python -m pytest -q tests/longhorizon
# 99 passed

uv run ruff check src/my_coding_agent/longhorizon tests/longhorizon
# All checks passed
```

The tests cover strict role JSON, Auditor evidence requirements, fresh Sessions,
tool allowlists, safe-shell rejection, hash-chain tampering, torn journals,
checkpoint drift, snapshot restore, event ordering, clean-audit completion,
follow-up consume-once, dangling-round resume refusal, workspace drift,
checkpoint/final-checkpoint failure, CLI exit status, secret scrubbing and a
CLI -> Controller -> RoleBackend vertical slice.

## Real model smoke

Run: `smoke-mea-20261117-8`

Model: `deepseek-v4.1-flash`, temperature `0.2`, provider retries `0`.

Task: make a tiny workspace's authoritative pytest suite pass without changing
the test. The initial state failed during collection because `calc.py` lacked
`add`.

Observed result:

- round 1: Executor added `add(a, b)`; Auditor read both files, ran the tests
  and returned a clean complete audit;
- round 2: Manager used the verified audit and returned `done`;
- independent host rerun in the workspace: pytest passed;
- final reducer: `complete`, completed rounds `[1, 2]`;
- round checkpoints `0001`, `0002` and final checkpoint were present and their
  hashes validated.

Provider usage captured by the local observer:

- 15 generation calls, 15 `ok`, 0 errors/cancellations;
- 40,507 total tokens;
- 28,672 cache-read tokens;
- purposes: `longhorizon_manager_r1`, `longhorizon_executor_r1`,
  `longhorizon_auditor_r1`, `longhorizon_manager_r2`.

Langfuse cloud read-back:

- trace ID `d337f693f1e091ea1efa13f9e5f6289b`;
- one trace with 15 GENERATION observations;
- model `deepseek-v4.1-flash`;
- total usage 40,507 tokens, matching the local ledger exactly;
- all four role/round purposes share the same trace root.

The CLI also verified that a completed run cannot be resumed and exits with a
non-zero status.

## v1.1 final pause/resume, immutable-path, and observation validation

Date: 2026-11-18.

The evaluated state is the current worktree at HEAD
`9b1a7c499f13b014cc6f3d0bd5f677092ce1cac6` plus uncommitted v1.1 changes. This
section supersedes the earlier v1.1 candidate status while retaining every
iteration below as history.

### Final fixes

- TOCTOU checks compare the protected path and workspace against the same
  captured snapshot.
- A blocked run may still load despite later workspace drift, while resume
  remains blocked.
- Python 3.11 reparse-point and junction handling is covered.
- CLI immutable paths are processed lexically; drive-relative paths, `..`, and
  link components fail closed without candidate-path resolution.
- Resume rejects a stale pause value immediately after store integrity checks,
  before auth, model, backend, or controller construction.
- Role parsing accepts exactly one fenced JSON object and rejects ambiguous or
  invalid framing.

### Offline and compatibility validation

- The main agent reran
  `uv run --no-sync python -m pytest tests\longhorizon -o addopts=''`:
  **135 passed in 18.37s**, exit 0.
- Ruff passed; `uv.lock` was unchanged; `git diff --check` was clean.
- The earlier real run `smoke-mea-20261117-8` still loaded under the new code
  and its integrity check returned `complete` with completed rounds `[1, 2]`.

### Retained real iterations

`smoke-mea-v11-20261118-1` failed before the first model call because the
rebuilt virtual environment lacked the `langfuse` package. It made **0 model
calls** and was retained.

`smoke-mea-v11-20261118-2` succeeded before the P1 fixes. It is retained as an
earlier successful candidate, not final-head evidence:

- 15 model calls, all 15 `ok`;
- 34,510 total tokens and 24,704 cache-read tokens;
- Langfuse trace `38e1f70eabc1af680fd09db128c9998c`;
- a 2026-11-18 cloud read-back once showed 15 GENERATION observations whose
  usage matched this run's local ledger.

`smoke-mea-v11-20261118-4` ran after P1 but before compatibility with exactly
one fenced JSON block. The Auditor returned explanatory text plus one fenced
JSON block, which the then-current parser treated as invalid JSON. The run
made 14 calls, all 14 were `ok`, and consumed 38,893 tokens. It recorded
`round_unknown` and `run_blocked`, produced no checkpoint, and was retained
without retry.

### Final run and cross-process recovery

`smoke-mea-v11-20261118-5` is the final evidence run. It used
`deepseek-v4.1-flash`, temperature `0.2`, and provider retries `0`.

The first independent process ran with `--pause-after-round 1` and returned:

- status `ready`;
- completed rounds `[1]`;
- next round `2`;
- only checkpoint `0001`.

An independent second process resumed and reached status `complete` with
completed rounds `[1, 2]` and checkpoints `0001`, `0002`, and `final`. A further
resume exited with code 2, so the completed run was not replayed. A host pytest
rerun in the final workspace exited 0.

The final local ledger contains:

- 16 generation starts, 16 ends, and 16 `ok` outcomes;
- **42,271** total tokens and **31,104** cache-read tokens;
- purpose counts: `manager_r1=1`, `executor_r1=9`, `auditor_r1=5`,
  `manager_r2=1`.

The Langfuse upload attempt completed and local observation remained normal:
the observer closed successfully with 0 dropped records and 0 export errors.
Node `langfuse-cli/fetch` and `curl` both failed at the host DNS resolver, so
the final proof uses a read-only PowerShell/.NET request to the public
`/api/public/v2/observations` REST endpoint as a resolver fallback.
That read-back returned trace `da8624e24bf352085623f9510fe868ea` with 16
observations, all 16 GENERATION, all status `ok`, 16 distinct observation IDs,
model `deepseek-v4.1-flash`, and totalUsage 42,271. Its purpose counts were
manager round 1 = 1, executor round 1 = 9, auditor round 1 = 5, and manager
round 2 = 1, exactly matching the local ledger.

### Immutable-path integrity

The SHA256 of `test_calc.py` was
`fa3379b77a73726a9db77c0f79c3ee954aadac6944c4daf8b69a20ee59e07d1c`
both before and after the final run. The current workspace file and the
persisted round-1 snapshot contain the same hash.

The final evidence proves pause/resume across separate CLI processes,
closed-round checkpointing, immutable-path preservation, clean local observation
shutdown, and a verified Langfuse read-back for one tiny task. It does not
establish a quality improvement, benchmark gain, or generalized long-horizon
capability.

## Delivery validation

Pull request: [PR #3](https://github.com/294962silent/LongArc/pull/3),
stacked on `codex/interview-harness-redesign` and not merged automatically.

Windows matrix at head `dd842b5d63951ce728436c32f714909d125a6046`:

- Python 3.11 job: success;
- Python 3.13 job: success;
- both included dependency sync, Ruff, pytest, TUI build/tests and wheel entry
  verification.

CI run:
https://github.com/294962silent/LongArc/actions/runs/44119193048

## Failed iterations retained

Seven earlier smoke attempts failed closed or were superseded and were not retried:

- `smoke-mea-20261117-1`: optional Langfuse dependency was absent; 0 model calls.
- `smoke-mea-20261117-2`: ContextVar token leaked across asynchronous yields;
  2 known calls / 2,998 tokens.
- `smoke-mea-20261117-3`: same bug on the non-TTY path; 2 known calls /
  2,882 tokens.
- `smoke-mea-20261117-4`: Auditor spent its turn budget after safe-shell denials;
  22 known calls / 71,466 tokens.
- `smoke-mea-20261117-5`: Auditor incorrectly placed non-blocking caveats in
  `remaining`; the round was closed, but the old journal predates the final
  strict schema and is now audit-only.
- `smoke-mea-20261117-6`: end-to-end smoke passed before the final Auditor
  isolation and Session-evidence gates; 24 calls / 72,972 tokens. It is retained
  as a superseded successful run because its manifest predates the frozen
  initial-workspace hash.
- `smoke-mea-20261117-7`: Manager completed, then an internal iterable error
  blocked the executor before any checkpoint. The diagnostic error evidence was
  added and the run was not retried.

Across all smoke attempts, the local ledgers contain 235,599 provider-reported
tokens; every settled call is `ok`. This is provider usage accounting, not a
verified vendor invoice.

Machine-readable evidence:
[summary.json](../results/longhorizon-mea-v1-20261117/summary.json).

## Limits

- The first version is CLI-only and is not an OS sandbox. Executor tools are
  bound to the workspace, but external commands remain host processes.
- Auditor commands and reads are bound to a copied snapshot. The original
  workspace is also snapshotted before and after audit; any observed mutation
  blocks progress. This is process-level isolation, not an OS sandbox.
- A crash inside a model/tool round is not exactly-once. Such a round remains
  `unknown/blocked` and requires operator investigation.
- Workspace snapshots are bounded at 5,000 files / 64 MiB by default.
- The real smoke covers one tiny task. It proves the mechanism, not broad
  long-horizon quality or a benchmark improvement.
- RSI remained paused; this layer does not use the RSI policy or claim a
  self-evolution result.

## Interview-ready technical points

1. Why external verified state: prevents incorrect self-assessment and context
   drift from compounding across turns.
2. Why fresh sessions: avoids carrying old trajectories and prompt prefixes into
   each bounded action while preserving evidence through cited audit records.
3. Why hash-chained WAL plus checkpoints: separates durable facts from chat
   text and makes recovery auditable.
4. Why `started -> unknown` instead of implicit retry: prevents duplicate paid
   calls and repeated side effects after uncertain crashes.
5. Why Auditor is separate: turns completion from an Executor claim into an
   environment-backed decision.
6. Why Langfuse usage was matched locally and remotely: observability is
   verified, not inferred from a successful SDK call.
