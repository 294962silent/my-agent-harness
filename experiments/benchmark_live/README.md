# Official task evaluation entry

Current adapter protocol is **v2**. Both arms create the same run-owned store and
register the production `artifact_read`/`artifact_search` tools with identical
schemas; only Context artifact projection changes on/off. Handles remain bound to
that store's owner and the factory accepts no arbitrary host file path. Protocol
v1 model trials omitted those readback tools and did not activate externalization;
their scores establish pipeline smoke only, not an Artifact ablation or token
savings. They must remain labeled v1 and must not be silently relabeled v2.

`test_benchmark_live_artifacts.py` separately verifies v2 locally with a fake
provider: a 14KB shell response hides a middle error marker in the projected view,
the next model step obtains its handle and invokes the actual production
`artifact_read`, and the following step sees both middle and final markers. The
off arm sees the complete source and has the exact same tool schemas. These local
fixtures make no real provider requests; their injected usage is synthetic.

`experiments.benchmark_live.harbor_agent:BudgetedHarborAgent` is a host-side Harbor
0.25.0 custom agent using the production Agent/ContextManager/Session and a single
container shell tool. The model receives Harbor's instruction and container output;
the adapter never reads solution or verifier content into its prompt. Harbor must
create a fresh model trial after the separate oracle trial; never reuse an oracle
container or mount host task/test/solution directories into the model container.

Run an unchanged official task with Harbor's `oracle` agent first. An actual
`result.json` must report oracle agent, reward 1, finished timestamp, no exception,
and a task checksum matching the current Task. The checksum includes official
solution/verifier data but those bytes are never prompt input. Dataset version,
source URL/commit/license and task selection must be preserved by the caller.

Linux host environment requires the project dependencies plus Harbor. From repo
root use `PYTHONPATH=src:.`. Inject `LIVE_BENCH_API_KEY` into the host process only,
without CLI literals, `--agent-env`, dotenv files, container environment or output.
Do not print the environment. Example after the oracle gate passes:

```sh
harbor run --path "$OFFICIAL_TASK" \
  --agent experiments.benchmark_live.harbor_agent:BudgetedHarborAgent \
  --model openai/deepseek-v4.1-flash --n-concurrent 1 --n-attempts 1 --max-retries 0 \
  --agent-kwarg task_dir="$OFFICIAL_TASK" \
  --agent-kwarg oracle_result="$ORACLE_RESULT" \
  --agent-kwarg ledger_path="$SHARED_LEDGER" \
  --agent-kwarg artifact_projection=on --jobs-dir "$RESULTS"
```

Use the *same absolute ledger path* for every task/arm/repeat in this authorized
$30 campaign. Compare `artifact_projection=on` and `off` with identical official
task, model, 32k context budget, tools and limits; this tests artifact projection,
not the entire framework. Sixteen LLM requests maximum (including summaries),
4096 output tokens/request, zero SDK/Harbor retries, and 600 seconds per agent run.
All five LLM entry methods use explicit reservations; the live adapter uses async
requests. Synchronous guard calls have pre-call deadline checks only.

The durable locked ledger atomically reserves full-price input/output costs at
$0.30/$1.20 per million. Serialized UTF-8 bytes plus framing allowance is a
conservative text-token ceiling assumption, not an absolute provider billing
guarantee. Unknown usage retains the reservation and blocks the affected run;
bound overruns block continuation and retain actual observed cost. Never treat
unavailable usage as zero or sum partial known rows into a complete task total.

`live_run.json` stores sanitized run IDs, arm, oracle evidence, actual stop reason
and request count. Harbor `result.json` holds the official score and aggregate
usage. `tasks.parse_trial_result` classifies pre-agent environment errors separately
from scored trials and leaves unavailable score/usage null. Local ledger/host logs
are evaluation evidence; no leaderboard upload is performed. Network/image build
failures are environment evidence, not model failure. No official tasks, gold
patches or hidden tests are bundled in this source tree.

## Explicitly pinned SWE entry

`python -m experiments.benchmark_live.swe_agent` supports only the official
`psf__requests-1963` and `sympy__sympy-20590` instances, with explicit base commits,
original image names and config IDs pinned in `swe_agent.py`. It is not a general
SWE dataset adapter. The safe metadata accepts only instance_id/problem_statement/
repo/base_commit/image/version/environment_setup_commit; never pass the full
evaluator metadata containing gold or test patches.

```sh
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_agent \
  --metadata "$SAFE_METADATA" --gold-evidence "$GOLD_EVIDENCE" \
  --ledger-path "$SHARED_LEDGER" --arm on --output "$UNIQUE_NEW_OUTPUT"
```

The gold evidence must bind the safe metadata SHA, image name/config ID, base
commit, and official report SHA; resolved must be true. The official report must
have completed=1/resolved=1/error=0 and contain this instance in resolved_ids.
Failed gold preflight is rejected before credentials, ledger, Docker or model
initialization. The requests1963 gold preflight timed out and therefore does
**not** unlock that task. Each allowed task requires its own successful evidence.

The solver creates a new container with `--pull never --network none`, no host
mounts or host environment forwarding, resets its ephemeral /testbed to the pinned
task base like the official evaluator, then verifies HEAD and clean status before
constructing the LLM. The original prebuilt image can contain an earlier HEAD;
that does not change the task base. A credential-free `probe_environment` function
records the initial image HEAD, verified base, initialization stage and cleanup
without creating a ledger or LLM. Solver failures persist stage/error_code and
explicit pre_agent_error when initialization failed before Agent construction.
Shell commands run only through Docker exec, with an inner
55 second timeout and five second kill grace. Final extraction uses intent-to-add
plus `git diff --binary <fixed-base>` so new files and model-created commits are
included. The result is `model_patch.jsonl` in SWE's prediction format, alongside
sanitized `run.json` and the local Agent session. This does not assign a score;
the owner runs SWE-bench 5.0.2 independently against the official evaluator data.
The solver removes its own fresh container in cleanup and never reuses a gold
container. The same limits, endpoint, credential injection and shared ledger
requirements above apply.
