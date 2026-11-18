# Long-horizon quality Harbor pilot attempt

Date: 2026-11-18

Status: integration completed; the preregistered 18-trial run is blocked by the
model provider's monthly quota. This document records partial and failed runs.
It is not a completed quality result.

## Frozen comparison

The target experiment is three Terminal-Bench 2.1 tasks (`fix-git`,
`large-scale-text-editing`, `custom-memory-heap-crash`) under two arms:

- `react`: one tool-using Agent with one append-only session.
- `mea`: a no-tool Manager, a fresh Executor session per round, and a fresh
  read-only Auditor session per round.

Each arm gets three trials, for 18 slots total. Both arms use
`deepseek-v4.1-flash`, the official task image and verifier, a 32,000-token
context budget, 4,096 output tokens, an Agent request cap of 64, zero provider
retries, and required Langfuse metadata observation.

The task order and each task's arm order are frozen from one seed. The three
trials for one task and arm run contiguously. This ordering keeps the same
system prompt and tool schema adjacent while preserving all preregistered
trials. The cache ratio is reported as:

```text
cache_read / (net_prompt + cache_read + cache_write)
```

The first request in each new role/session is expected to be cold.

## v9: two-round MEA protocol failure

The first campaign reached Harbor and the model. Four slots completed before a
terminal-state defect was found:

| Task | Arm | Trial | Official reward | Requests | Cache hit |
|---|---|---:|---:|---:|---:|
| fix-git | react | 0 | 1 | 22 | 76.3168% |
| fix-git | react | 1 | 1 | 22 | 82.4113% |
| fix-git | react | 2 | 1 | 21 | 80.4329% |
| fix-git | mea | 0 | 1 | 51 | 70.3281% |

The three `react` trials averaged 79.7203% cache hit. The completed `mea` trial
used three fixed role prompts and fresh role sessions; its lower single-trial
rate is descriptive and cannot support an arm comparison.

The MEA trial ended in orchestration status `blocked` even though the official
verifier returned reward 1. Its second Auditor round returned
`status=complete` and `remaining=[]`, but the two-round limit did not leave a
Manager turn to emit the required terminal `done` decision. The protocol was
corrected to allow three Manager/Executor/Auditor rounds. The v9 record is
retained and excluded from the final pilot.

An already-started `fix-git/mea/1` slot was cancelled when the defect was
confirmed. It recorded 18 successful requests and one cancelled request, with
73.6714% cache hit on the known requests. It has no official reward and is not
retried.

Across the four completed v9 slots, the local ledger contains 116 requests and
784,124 provider tokens: 170,191 net prompt, 552,448 cache-read, and 61,485
output tokens. The client-side fixed-price estimate is `$0.290618`; this is not
provider billing evidence.

Langfuse read-back returned one trace per run and exactly 22, 22, 21, and 51
`GENERATION` observations. Cloud usage matched the local ledger for every
completed run. The cancelled v9 slot also produced one trace with 19
`GENERATION` observations, matching its 19 ledger requests.

## v11: provider quota block

The corrected campaign passed source/task/oracle hashes, Docker transport,
proxy, credential, and Langfuse preflight. It then attempted four model calls.
Every call failed at the provider boundary:

- three slots finished as `execution_or_verifier_error`;
- one slot was started and then closed after the systematic failure was found;
- each ledger has one `error` request and no usage or cost value;
- no official verifier ran and no task reward exists.

A direct diagnostic without the Agent reported an OpenCode Go
`GoUsageLimitError` HTTP 429: the monthly usage limit was reached with an
estimated reset in 4 days 23 hours. No account identifier is retained here.

Langfuse read-back found exactly four traces and four `GENERATION`
observations, one per failed request. Their usage fields are empty, consistent
with a request rejected before generation. The run is fail-closed and its run
IDs will not be reused.

## What this establishes

- The three-task/two-arm plan, official task pins, request/usage accounting,
  required Langfuse scope, role prompt hashes, and cache counters are wired
  through Harbor.
- A real MEA trial reached the official verifier and passed it, but the
  two-round configuration was not a valid terminal protocol.
- The corrected implementation has one isolated provider blocker: the account
  quota, not Harbor, Docker, Langfuse, the task images, or the MEA parser.
- Stable-prefix ordering and fixed role prompts produced 70% to 82% observed
  cache-hit rates in the v9 slots. There is no controlled ordering experiment,
  so this is not evidence that the ordering caused a quality or cost gain.

## What this does not establish

- It does not establish that MEA is better or worse than `react`.
- It does not establish a long-horizon quality improvement.
- It does not establish a Terminal-Bench 2.1 population-level result.
- It does not establish provider billing, because failed requests have unknown
  usage and the successful costs use a fixed client price table.

After the provider quota is available, a fresh campaign ID must be frozen
without changing the task selection, model, prompt hashes, budgets, or outcome
schema. The three v11 failed run IDs and the v9 partial run IDs must not be
reused.
