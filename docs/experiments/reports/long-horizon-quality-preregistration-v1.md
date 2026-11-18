# Long-horizon quality comparison pre-registration v1

Date: 2026-11-18

Status: protocol and offline analysis implemented; no quality result is claimed.

## Question

Does the Manager -> Executor -> Auditor runtime improve official long-horizon
task success when compared with the same harness running one ReAct agent?

Two separate questions are kept separate:

1. Does the full system solve more official tasks under the same model, tools
   and total budget?
2. Do checkpoint/resume and immutable-input enforcement repair specific
   failures after interruption or attempted input mutation?

Question 2 is a mechanism experiment. It cannot be merged into the task score
to manufacture a quality improvement.

## Frozen arms

| Arm | Execution | Manager | Auditor | Fresh sessions | Resume | Immutable path |
|---|---|---:|---:|---:|---:|---:|
| `react` | one ReAct agent | no | no | no | no | no |
| `mea` | Manager -> Executor -> Auditor | yes | yes | yes | yes | yes |

Both arms must use:

- the same provider model and endpoint;
- the same executor shell and artifact tools;
- the same temperature, output cap and retry policy;
- the same context, request, time and cost ceilings;
- the same versioned role prompts and tool-schema serialization;
- the same official task revision, container image digest and verifier;
- new agent sessions at the same task boundary;
- no access to reference solutions or hidden scoring files.

The manager is tool-free. The auditor receives a separate context and may only
inspect evidence or run the pre-approved checks. Every paid model request must
be reserved and settled through the existing durable budget ledger.

## Cache-stability contract

The comparison must not turn a quality experiment into an accidental cache
experiment.

- Each role uses one versioned system-prompt constant for the entire campaign.
- Tool names, descriptions, schemas and serialization order are frozen before
  the first paid request.
- Timestamps, run IDs, task IDs, scores and changing runtime state are never
  placed in the stable prompt prefix. They are appended in the current user
  turn, or represented as a new state snapshot only when the state changes.
- Within one agent session, history is append-only. Completed tool outputs are
  not rewritten later merely to shorten a subsequent prompt.
- Model, endpoint, temperature, max output, retry policy and provider options
  are frozen across both arms and all trials.
- Every execution receipt records `system_prompt_sha256`,
  `tool_schema_sha256`, `model` and provider-returned cache usage.
- Prefix changes are diagnosed with a reason such as model change, tool-schema
  change, system-prompt change, new summary, artifact projection or branch
  switch.

For each arm and trial report:

`cache_hit_rate = cache_read_tokens / (prompt_tokens + cache_read_tokens + cache_write_tokens)`

Use the same provider definition for both arms. If any denominator field is
unavailable, cache-hit rate remains unavailable rather than estimated.
New sessions are required at task boundaries, so the first request in each
trial is expected to be cold. Cache-reuse claims apply only to the remaining
requests in that frozen session.

## Task sets

The preview/pilot set uses the three Terminal-Bench 2.1 tasks already covered
by zero-model oracle preflight:

- `fix-git`;
- `large-scale-text-editing`;
- `custom-memory-heap-crash`.

The manifest is frozen in
`docs/experiments/plans/terminal-bench-21-quality-pilot-v1.json`.

A three-task pilot is descriptive only. It exists to validate the adapter,
ledger, Langfuse correlation, task identity and reporting. It cannot support a
benchmark-quality claim.

The confirmatory campaign must freeze all 89 Terminal-Bench 2.1 tasks before
its first paid trial. The manifest, ordering, revision, model, limits and run
IDs must be written to `plan.json` and hashed. No post-result task replacement
is allowed.

Recovery and immutable-path cohorts use separate task manifests and separate
claims. The primary quality score never includes scripted fault injection.

## Factorial schedule

Each task and arm receives at least three trials. The task-arm-trial slots are
shuffled once with a frozen seed. The same task must appear in every arm and
trial.

Initial pilot:

`3 tasks x 2 arms x 3 trials = 18 scored trials`

Confirmatory run:

`89 tasks x 2 arms x 3 trials = 534 scored trials`

Every trial gets a deterministic run ID. A started or unknown trial is never
silently retried under the same ID.

## Metrics

Primary metric:

- `official_rate`: mean official verifier reward, where reward is binary.

Secondary metrics:

- `eventual_pass`;
- `false_done`: the agent reports completion but the official verifier fails;
- `manual_intervention_count`;
- settled requests, total tokens, cache-read tokens and cost;
- wall-clock time;
- for the separate recovery cohort: recovery success, replay count and
  duplicate side effects;
- for the separate integrity cohort: attack block rate and legitimate-change
  false-block rate.

Usage is known per request or marked unavailable. Unknown usage is never
estimated as zero and closes the relevant confirmatory gate.

## Statistical analysis

Trials are aggregated within task before comparisons so repeated stochastic
runs do not become artificially independent samples.

The primary analysis computes the per-task `mea - react` reward difference,
then reports:

- mean paired difference;
- 95% task-cluster bootstrap confidence interval;
- exact two-sided sign test over nonzero task differences;
- request, token and cost ratios.

A confirmatory quality claim requires:

1. all 89 frozen tasks are present;
2. every task-arm-trial slot has an official scored result;
3. no infrastructure-blocked, suspected-verifier or unknown-usage slot;
4. mean paired difference is positive;
5. the lower bound of the 95% cluster-bootstrap interval is above zero;
6. the exact sign-test p-value is below 0.05.

An efficiency claim is separate. It requires non-inferior quality within the
pre-frozen margin and a lower request or cost ratio.

## Cost accounting

Before the confirmatory run, estimate:

`requests = sum(observed mean requests per task and arm x trials)`

`cost = sum(settled input/output tokens x frozen provider prices)`

The estimate is planning evidence, not an invoice. Actual cost comes from the
durable ledger. Cache usage remains unavailable if the provider does not return
all required fields.

## Commands

```powershell
uv run python -m experiments.long_horizon_quality freeze `
  --task-manifest docs\experiments\plans\terminal-bench-21-quality-pilot-v1.json `
  --output <new-private-campaign-dir> `
  --campaign-id long-horizon-quality-pilot-v1 `
  --stage pilot `
  --trials 3

uv run python -m experiments.long_horizon_quality analyze `
  --results <trial-outcomes.json> `
  --plan <new-private-campaign-dir>/plan.json `
  --json <summary.json>
```

The current implementation freezes slots and performs strict offline paired
analysis. Harbor/MEA execution is wired in the next implementation increment.

## Claim boundaries

- The pilot can validate execution and report descriptive observations only.
- A positive pilot cannot be extrapolated to the 89-task benchmark.
- A checkpoint or immutable-path fault experiment proves that mechanism, not
  end-to-end model quality.
- The historical 71/89 run and the previous three-task pilot are not rerun or
  reinterpreted by this protocol.
- Until the confirmatory gate passes, the correct wording is "implemented and
  experimentally instrumented", not "improved long-horizon quality".
