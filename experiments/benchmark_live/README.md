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

## Frozen catalog SWE entry

`python -m experiments.benchmark_live.swe_agent` accepts approved official tasks
through `swe_catalog.py`. A catalog records the official task repository/revision,
instance ID, repo, base commit, original image/config ID and safe metadata SHA256.
The caller must supply the catalog SHA256 frozen by the trusted host preparation
step. This detects input changes; it is not a signature against a malicious host.
Unknown fields, mismatched bindings and gold/test fields are rejected.

Without a catalog, the legacy `psf__requests-1963` and `sympy__sympy-20590`
bindings remain available. The safe metadata accepts only
instance_id/problem_statement/repo/base_commit/image/version/environment_setup_commit;
never pass the full evaluator metadata containing gold or test patches.

```sh
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_agent \
  --metadata "$SAFE_METADATA" --gold-evidence "$GOLD_EVIDENCE" \
  --catalog "$CATALOG" --catalog-sha256 "$CATALOG_SHA256" \
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

`--spend-ceiling-usd` optionally imposes a lower absolute cumulative ceiling on
the same ledger, checked under its reservation lock for every request. The ledger's
original $30 policy remains intact. For a campaign starting after $0.068597 in
known settled costs, a $3 additional allowance gives a $3.068597 ceiling. This is
client-side accounting, not a verified provider bill. `--run-id` allows the host
campaign to persist a request correlation ID before the solver starts.

Catalog support does not imply the complete benchmark has been prepared or run.
Task selection, official input hashes, image availability and gold preflight must
still be frozen separately. See the six-task [v2 selection](../../docs/experiments/results/swe-v2-pilot-20261112/SELECTION.md).

## Frozen small campaign

`swe_campaign.py` provides `plan`, `run`, `score` and `summarize`. Prepare all
official gold results first, including failed preflight evidence. The task-inputs
JSON contains `tasks`, each with instance_id, metadata, gold_evidence,
evaluation_dataset and evaluation_cwd. Safe metadata alone reaches the solver;
the full evaluation dataset goes only to the separate official scorer.

```sh
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_campaign plan \
  --task-inputs "$TASK_INPUTS" --catalog-path "$CATALOG" \
  --catalog-sha256 "$CATALOG_SHA256" --ledger-path "$SHARED_LEDGER" \
  --batch-budget-usd 3 --output "$NEW_CAMPAIGN_DIR"
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_campaign run \
  --campaign "$NEW_CAMPAIGN_DIR" --max-new-trials 2
# Inspect the first complete pair, then resume the remaining frozen trials.
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_campaign run \
  --campaign "$NEW_CAMPAIGN_DIR"
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_campaign score \
  --campaign "$NEW_CAMPAIGN_DIR"
PYTHONPATH=src:. python -m experiments.benchmark_live.swe_campaign summarize \
  --campaign "$NEW_CAMPAIGN_DIR"
```

The output directory must not exist when planning. It receives `plan.json` and
`plan.sha256`, freezing input/source hashes, seed23 ordering, limits and initial
budget state. Each task has one on/off pair; first-arm assignments are balanced.
`run` is serial and guarded against concurrent execution of the same campaign.
It persists started/run IDs before paid work. Complete hashed results may be
skipped on resume; interrupted or failed trials are retained without automatic
retry. Unknown usage blocks continuation rather than becoming zero cost.
`--max-new-trials N` optionally bounds this invocation's new eligible executions;
completed and blocked entries do not count. It does not rewrite the plan or
automatically rebalance a partially completed pair.

`score` invokes SWE-bench 5.0.2 independently with the model credential removed.
Cancellation reaps the child process group and cleans only the container matching
that trial's instance/run ID. Summary separates selected, eligible, execution
attempts, actual model attempts, scored and resolved trials. Artifact activation
and readback are reported separately; inactive interventions cannot establish
causal savings. These commands do not run the complete 300-task benchmark.
