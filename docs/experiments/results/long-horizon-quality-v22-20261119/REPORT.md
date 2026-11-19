# Long-Horizon Quality Pilot v22

## Status

**Sealed as a protocol and infrastructure qualification attempt. It is not a
quality experiment.**

Campaign: `lhq-tb21-pilot-20261119-v22`

Frozen source: `75d1578`

Claim ceiling: `descriptive_only`

The campaign was stopped after 10 of 18 slots had been attempted. The earlier
slots used the original verifier network setup; later slots used a temporary APT
overlay. That change was made after `apt-get update` hung against the Ubuntu
archive. The run IDs already consumed under the first setup were not reused.

The mixed network setup and incomplete cohort invalidate pass-rate, significance,
React-versus-MEA, cache-quality, and long-horizon quality claims. All artifacts
remain in `summary.json`, `journal.json`, and `ledgers/`.

## Observed outcomes

| Task | Arm | Trial | Observed category | Reward | Requests |
|---|---|---|---:|---:|---:|
| fix-git | React | 0 | scored | 1 | 14 |
| fix-git | React | 1 | scored | 0 | 17 |
| fix-git | React | 2 | scored | 1 | 17 |
| fix-git | MEA | 0 | scored | 1 | 63 |
| fix-git | MEA | 1 | execution/verifier error | unavailable | 6 |
| fix-git | MEA | 2 | scored | 1 | 50 |
| custom-memory-heap-crash | MEA | 0 | scored | 0 | 63 |
| custom-memory-heap-crash | MEA | 1 | scored | 0 | 63 |
| custom-memory-heap-crash | MEA | 2 | execution/verifier error | unavailable | 18 |
| custom-memory-heap-crash | React | 0 | interrupted after start | unavailable | 31 |

Totals:

- 7 slots reached official scoring; 4 scored reward 1 and 3 scored reward 0.
- 2 slots ended in execution or verifier errors.
- 1 slot started and was interrupted during the approved stop.
- 8 slots were never started.
- 342 provider requests were recorded across attempted slots.
- 8 slots had complete usage records: prompt 421,167, completion 247,683,
  cache-read 1,086,080, cache-write 0, and observed cache hit **72.0572%**.
- Known conservative client cost across those complete rows was **$0.749525**.
- Two attempted slots retained unknown usage/cost; unknown values were not
  imputed as zero.
- Langfuse metadata-only readback matched all 10 attempted sessions: 342 remote
  observations for 342 local requests. The readback excludes prompt and answer
  bodies; missing usage remains unavailable.

## Failure chain

1. `custom-memory-heap-crash/MEA/t0` reached the official verifier but hung in
   `apt-get update` because the frozen job left `HTTP_PROXY` empty while Ubuntu
   archive HTTP was unreachable. The slot is retained with
   `suspected_verifier_infra=true`.
2. A temporary APT mirror/proxy repair allowed dependency installation, but the
   next slot failed when the uv installer could not reach
   `release-assets.githubusercontent.com` over TLS.
3. A preinstalled uv/Python/pytest bootstrap fixed that layer for the next
   attempt, but an Auditor JSON-projection provider request then failed and the
   ledger correctly blocked the campaign with unknown usage.
4. The next React attempt had a long pending provider request. The cohort was
   stopped rather than changing the bootstrap again inside the same campaign.

The `fix-git/react/t1` reward 0 and both official custom-memory reward-0
results are retained, but their verifier diagnostics show missing uv/verifier
execution for the infrastructure-affected rows. They are not valid evidence that
the model failed the task.

## Evidence

- `plan.json` and `journal.json`: exact frozen slots and terminal states.
- `ledgers/`: all 18 pre-created ledgers, including unknown and unsettled usage.
- `summary.json`: conservative per-slot extraction and category counts.
- `langfuse-readback.json`: remote session/observation and usage metadata.
- `SHA256SUMS`: integrity manifest for this archive.

The unmodified private campaign tree remains under
`.git/codex-delivery/long-horizon-quality/pilot-v22`.

## Permitted statement

The run demonstrated that the Harness can journal real Terminal-Bench executions,
preserve exact run identity, expose verifier bootstrap failures, and fail closed
on unknown provider usage. It did not produce a valid quality comparison.
