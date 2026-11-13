# Opt-in serial RSI runtime

`CodingAgent(..., rsi_runtime=runtime).run(goal)` now dispatches real child
CodingAgents through the existing core Agent loop. The default remains ordinary
single-agent execution. Enabled RSI `run_stream()` explicitly raises
`NotImplementedError`; it does not silently bypass the portfolio.

## Construction and review

```python
from my_agent_core.context_compaction import CompactionPolicy
from my_coding_agent.rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
from my_coding_agent.rsi_runtime import FrozenPolicy, ParentBudget, RsiRuntime

budget = ParentBudget(max_requests=12, max_tokens=300_000,
                      max_seconds=90, max_evaluations=6)
factory = LocalBranchFactory(source, new_output_directory,
    compaction_policy=CompactionPolicy(mode="layered", model_context_window=32000))
runtime = RsiRuntime(factory=factory, budget=budget,
    evaluator=VisibleCommandEvaluator([python_executable, "visible_tests.py"]),
    policy=FrozenPolicy({"max_width": 2, "max_refine": 1}),
    task_id="explicit-task-id", split="test", schedule="rsi")
```

Pass `rsi_runtime=runtime` to a CodingAgent whose workspace is exactly `source`.
Use a new output directory outside that source. The host supplies the visible
command and source snapshot. Each branch has its own workspace, Session and
ArtifactStore; refinements reuse only that branch's history. These directories
are **not an OS sandbox**. Tools and subprocesses retain host permissions.
Symlink, Windows junction and reparse paths (including lexical ancestors) are
rejected before resolution/copy/application; relative inventory paths reject
drive/UNC/absolute/traversal forms. These checks do not remove all filesystem
TOCTOU races or supply OS isolation. MCP, skills, plugins, optional memory/tasks
and automatic retries are disabled in children in this first implementation.
The demo therefore does not exercise memory autodistillation. In ordinary
configured agents, `register_completed_prefix` still invokes the existing
`before_compact` memory checkpoint even with a supplied summary; that checkpoint
can make additional fact-extraction LLM calls. A zero summary-call counter is
not evidence that every LLM call was zero. The RSI shared facade counts any
async calls that reach it; this demo makes no claim about enabled memory hooks.

After `await agent.run(goal)`, inspect `agent.last_rsi_result`: `selected_path`,
`patch_path`, `changes_path`, `trace_path` and `report`. No selected tree is
automatically copied to the original source. Explicit
`result.apply_to(source)` checks the original inventory, selected inventory and
changes manifest before application. This is a local review helper, not an
atomic filesystem transaction or a Git merge. Cancellation propagates after
cleanup and writes a cancelled report; facade `abort()` cancels the active RSI
episode. Runtimes are single use, with no implicit resume or retry.

## Shared ceilings and evidence

All child requests and ContextManager summaries use the same `SharedLLM` and
parent pool. Requests reserve input UTF-8 bytes plus framing and bounded output
before dispatch; this is an explicit conservative protocol assumption, not a
tokenizer guarantee. Known normalized usage settles the reservation. Cancelled,
failed or incompletely reported requests retain their reservation, block further
dispatch and never become fabricated zero usage. Authoritative total minus
completion includes separately reported cached input. Missing cache fields are
not assumed zero when total usage is absent. Known output-policy violations
retain actual settled usage and stop the parent. Flat explicit prices are
optional; unavailable cost stays null. Sync provider entry points are unsupported.
The async streaming facade buffers a batch response; it does not claim actual
incremental provider streaming.

The same deadline and evaluation pool cover the baseline and branch visible
commands. Independent final commands, when run by the demo host after selection,
use that same pool and a copied selected tree. Final feedback is excluded from
online selection and optimizer data. `final_success` remains null and quality
is unmeasured. Reports distinguish planned, dispatched, completed, valid,
invalid and undispatched attempts, terminal reasons, usage, process cleanup and
artifact hashes. Only fully valid scored execution prefixes can export traces.
Model/provider source hashes are host provenance evidence, not cryptographic
proof that a remote service was used. Scripted/custom clients remain synthetic.

`FrozenPolicy.from_registry(path)` reads one active policy once and fails if none
exists. Explicit `FrozenPolicy(parameters)` also copies validated parameters.
No episode hot-swaps policies. The runtime and demo never promote a policy.

## Zero-provider closed-loop contract

With the optional pinned Dream SDK installed, from the repository root:

```powershell
.venv/Scripts/python.exe -m my_coding_agent.rsi_runtime demo --output NEW_OUTPUT_DIRECTORY
# Equivalent checked-in experiment entry:
.venv/Scripts/python.exe experiments/context_rsi/run_contract.py --output NEW_OUTPUT_DIRECTORY
```

The command creates actual file-editing CodingAgents with a scripted model,
runs real local visible tests, exports measured train/validation traces, invokes
the actual `dream_rsi_optimizer.optimize(..., dry_run=True)` replay path without
an injected evaluator, freezes selected parameters and executes four arms:

- A: baseline context and fixed width/refinement policy.
- B: layered context/artifact projection and the same fixed policy.
- C: B with the optimizer-selected frozen policy.
- random: B with seeded legal random scheduling under the fixed policy bounds.

All arms have identical parent request/token/deadline/evaluation ceilings and
tool schemas. Actual spending may differ; no dummy calls equalize consumption.
Each final test runs only after that arm's selection is frozen. `TaskSplits`
rejects task overlap. `export_dataset` verifies trace hashes, exports train and
holdout only, and delegates semantic scored-tree split validation to the
optimizer. Test tasks never enter optimization.

`demo-summary.json` records the complete comparison, provenance, budget records,
actual fold/micro activity and separate final results. `optimizer/` retains SDK
replay evidence; each arm retains plan, Sessions, attempt snapshots, tool action
events, trace and selected patch/tree. The fixture deliberately lowers the micro
threshold to one character so real projection paths execute. It is a **synthetic
integration contract**, not a benchmark, quality improvement, official hidden
test, API connectivity test or production policy promotion. No paid API calls
occur. Original source files are left untouched. No experiment module is imported
by production code.

## Ordinary RPC/TUI context configuration

`Settings.context_compaction` defaults to baseline. Global trusted settings may
select `mode="layered"` and explicit context window/reserves, recent tool groups,
micro threshold and summary ceiling. The shared RPC integration-options path
passes this policy through initial creation, rebuild and fork. Project settings
cannot override this privileged global configuration. This option changes
context projection only; it does not enable RSI automatically or alter provider
native cache state. Existing tool installation remains identical when artifact
projection is disabled. No user settings are modified by the demo.

Targeted regressions are in `tests/test_rsi_runtime.py` and
`tests/experiments/test_rsi_closed_loop.py`; the latter requires the optional SDK.

Snapshot/copy/export currently perform synchronous disk I/O and may block the
event loop. The parent rejects further requests/evaluations at the next budget
boundary; it cannot forcibly preempt every filesystem operation. See the
[evaluation protocol](experiments/RSI_CLOSED_LOOP_EVALUATION.md) and
[engineering discussion](interview/11-RSI闭环与本地推理工程深挖.md).
