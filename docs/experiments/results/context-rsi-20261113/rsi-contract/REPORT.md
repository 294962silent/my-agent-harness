# Serial RSI production-loop contract, 2026-11-13

This local Windows run used real production CodingAgents, actual write tools,
real subprocess tests and Dream RSI 0.1.0's recorded-score replay optimizer.
The LLM was scripted: **zero provider API calls, synthetic usage, unmeasured
quality, no policy promotion**. No paid benchmark or existing ledger was touched.

Reproduce from the repository environment, selecting a new output directory:

```powershell
.venv/Scripts/python.exe experiments/context_rsi/run_contract.py --output experiments/context_rsi/results/contract-20261113-serial-final
```

The complete local output is under that directory. Checked-in
`CONTRACT_EVIDENCE.json` records source/profile hashes, source versions, dataset
entry hashes, raw summary hash, terminal reasons, per-attempt context activity,
tool-schema hashes and shared budgets. Generated Sessions and workspace trees
stay local under ignored `results/`.

- Train/holdout traces are distinct actual scored trees. Test task IDs are
  excluded. The actual dry-run optimizer ran 21 replay evaluations, selected
  width 1/refinement 1 and left `promotion_eligible=false`, `promoted=false`.
  Its recorded-score validation gain is synthetic replay evidence only.
- A and B used the same fixed width 2/refinement 1 policy. C used the frozen
  optimizer-selected policy. Random used legal seeded scheduling (seed 23)
  under the fixed policy bounds. All arms used identical tool schemas and parent
  ceilings: 12 requests, 300,000 reservation tokens, 90 seconds, 6 evaluations.
- A/B/random completed four valid attempts and used 12 synthetic requests,
  60 scripted tokens and six real evaluations each. C completed two valid
  attempts, using six synthetic requests, 30 scripted tokens and four real
  evaluations. No dummy calls equalized spending; these are fixture outcomes,
  not real efficiency gains. Cost remains unavailable.
- B recorded two phase-fold views/four micro groups. C recorded one/two;
  random recorded two/four; A recorded zero/zero. The micro threshold was
  deliberately one character to exercise real projection paths.
- Each arm selected a visible score 1 candidate, then passed a separate local
  final command after selection was frozen. Final evaluation consumed the same
  parent pool, used a copy of the selected tree and never entered controller
  feedback or optimization. `final_success=null` and `quality=unmeasured` remain.
  The original test source remained `answer.txt = 0`; there was no auto-apply.

Verification commands and observed results:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_rsi_runtime.py tests/experiments/test_rsi_closed_loop.py -o addopts= -q
# 44 passed
.venv/Scripts/python.exe -m pytest tests/coding/test_agent.py tests/coding/test_coding_agent_e2e.py tests/coding/test_settings.py tests/coding/test_session_rpc.py tests/coding/test_session_tree_rpc.py tests/coding/test_rpc_server.py tests/coding/test_optional_integrations.py tests/coding/test_artifacts_integration.py tests/core/test_context_layered.py tests/core/test_agent.py tests/coding/test_dream_rsi_optimizer.py tests/coding/test_dream_rsi_replay.py --import-mode=importlib -o addopts= -q
# 190 passed
.venv/Scripts/python.exe -m ruff check src/my_coding_agent/rsi_runtime.py src/my_coding_agent/rsi_branch.py src/my_coding_agent/agent.py src/my_coding_agent/settings.py src/my_coding_agent/rpc/session_handlers.py tests/test_rsi_runtime.py tests/experiments/test_rsi_closed_loop.py experiments/context_rsi/run_contract.py
# All checks passed
```

The first combined regression collection omitted `--import-mode=importlib` and
failed because core and coding both contain `test_agent.py`. The corrected
command above completed. Cancellation regressions cover facade abort and direct
task cancellation, retained unknown usage reservations, failure denominators and
cleanup. Application guards reject changed source, selected files or manifests.
Final targeted coverage also applies the exported patch with Git and checks
exact bytes for no-final-newline, LF and CRLF combinations (checkout autocrlf
disabled for that assertion). Windows junctions are rejected at inventory,
copy, factory source/output and application boundaries, including lexical
ancestors before resolution. Relative inventory paths reject drives, UNC and
traversal. Cleanup failures cannot export optimization traces or report
completed; cancellation during cleanup still propagates. These checks do not
provide OS sandboxing, atomic application or removal of all TOCTOU races.
The 190-test expanded run preceded these final path/cleanup corrections; the
44-test targeted run and final CLI include them.

The demo disables optional memory. Ordinary `before_compact` memory checkpoints
can issue fact-extraction LLM calls even when a structured summary is supplied;
zero summary calls do not mean zero calls across the whole configured system.
This run does not claim memory-hook or Qwen/GPU validation. See
`docs/rsi-runtime.md` for the public interface and remaining limitations.

## Independent integration verification

The primary agent reran the final stable source in a new directory:
`experiments/context_rsi/results/contract-20261113-independent-final`.
[independent-validation.json](independent-validation.json) binds its raw summary
hash and confirms source hashes, policy selection, counts, shared ceilings,
post-selection final checks and unchanged source against the published evidence.
No paid provider calls occurred; quality remains unmeasured. The two runs are
integration repetitions, not independent benchmark tasks.

Final Windows all-extras verification used `--import-mode=importlib`:
70 targeted tests passed in16.37s (44 RSI plus26 memory-profile contracts),
then the complete suite passed: **1424 passed, 11 skipped, 5 subtests passed in107.00s**.
Ruff, frozen lock check and wheel module/source-byte verification passed.
The 190-test earlier run is historical, not the final total. Disk snapshot/copy
is synchronous and not forcibly preempted by the parent deadline; the next
provider/evaluation boundary still refuses overspending.

Packaging found CRLF source files would be normalized by Git. The original
run/source hashes remain unchanged in historical evidence;
[source-line-ending-map.json](source-line-ending-map.json) proves the five files
changed only CRLF to LF. Minimal gitattributes rules preserve the source bytes.
An actual autocrlf=true checkout of all11 referenced sources matched bytes/hash.
After normalization,44 targeted tests passed in17.98s and a third new CLI run
matched all published counts/ceilings:
[checkout-validation.json](checkout-validation.json). Its source hashes identify
the committed LF form. Rebuilt wheel modules match these final source bytes.
