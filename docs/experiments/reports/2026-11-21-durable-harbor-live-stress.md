# Durable Harbor live stress: 100 synthetic tasks

## Purpose

This experiment verifies the complete runtime path:

```text
SQLite durable queue
  -> bounded worker
  -> Harbor CLI and Trial
  -> MyAgentHarness
  -> LongArc Harbor Docker environment
  -> DockerSandboxExecutor
  -> container tool calls
  -> Harbor verifier
```

The task set is synthetic. It tests orchestration, isolation, cleanup, model
transport, and verifier integration. It is not a Terminal-Bench or SWE-bench
quality score.

## Configuration

| Item | Value |
| --- | --- |
| Tasks | 100 independent Harbor task directories |
| Model | `deepseek/deepseek-v4.1-flash` through OpenCode Go |
| Harbor | `0.24.0` |
| Per-task limit | 1 CPU, 512 MiB, 420 s durable timeout |
| Scheduler | 8 active jobs, 8 sandbox slots, 4096 MiB budget |
| Worker model | 8 SQLite workers; one Harbor Trial per claimed job |
| Base image | `ubuntu:22.04` |

Each task asks the Agent to create one file containing a unique token. The
verifier writes Harbor's `reward.txt`; a run receives reward 1 only when the
file and token are correct.

## Result

| Metric | Result |
| --- | ---: |
| Jobs submitted | 100 |
| Unique job IDs | 100 |
| Succeeded jobs | 100 |
| Failed / timeout / cancelled | 0 / 0 / 0 |
| Harbor Trials passed | 100 |
| Reward files containing 1 | 100 |
| First-attempt successes | 100 |
| Agent sessions | 100 |
| Agent tool calls | 166 |
| Sessions using tools | 100 |
| Input tokens | 56,228 |
| Output tokens | 14,155 |
| Cache-read tokens | 318,208 |
| Cache-read share of prompt tokens | 84.98% |

Duration includes Harbor process startup and container setup:

| Duration | Seconds |
| --- | ---: |
| Minimum | 63.91 |
| Median | 103.39 |
| p95 | 122.43 |
| Maximum | 131.72 |

The scheduler reached 8 active jobs, 8 sandbox slots, and 4096 MiB of logical
reservation. An independent `docker ps` probe sampled up to 4 simultaneously
running managed containers over 274 samples. The lower physical sample reflects
short container lifetimes and the sampling interval; it is not evidence of
eight simultaneous Docker containers.

After completion:

- scheduler active jobs: 0
- scheduler sandbox/memory usage: 0 / 0 MiB
- running `longarc.managed` containers: 0
- all `longarc.managed` containers: 0

## Recovery evidence

The run was deliberately kept on one run ID and one idempotency namespace.

1. After 64 successes, WSL restarted while SQLite was stored on the Windows
   `E:` 9p mount. The database remained valid; the next process resumed the
   same completed jobs without re-running them.
2. After 72 successes, SQLite returned `disk I/O error` on the 9p mount. A
   consistent backup was checkpointed to ext4 in WSL, and the remaining jobs
   resumed from the copied database.

The final 36 jobs therefore also exercise recovery and idempotent continuation,
not only a clean uninterrupted loop.

## Earlier failed attempts

These failures are retained rather than counted as model quality:

| Attempt | Failure | Resolution |
| --- | --- | --- |
| Initial live smoke | 401 because the OpenCode token was sent to `api.deepseek.com` | Set the OpenCode-compatible base URL |
| Durable smoke v1 | Invalid Docker bind mount field `rw` | Use Docker's `readonly` mount flag |
| Canary v1 | Harbor interpreted tasks as a dataset because `environment/` was absent | Add the required environment directory |
| Canary v2 | Verifier did not write `reward.txt` | Make every verifier write reward 0 or 1 |
| Canary v3 | CRLF changed the shell shebang to `/bin/sh\r` | Write task scripts as LF bytes |

## Evidence

- [Sanitized result JSON](../results/2026-11-21-durable-harbor-stress100.json)
- Raw summary: `.git/codex-delivery/harbor-live/stress100-v1-summary.json`
- Final SQLite export:
  `.git/codex-delivery/harbor-live/stress100-v1-final.sqlite`
- Harbor job artifacts:
  `.git/codex-delivery/harbor-live/stress100-v1-jobs`
- 8/8 canary:
  `.git/codex-delivery/harbor-live/canary8-v4-summary.json`
- Single real smoke:
  `.git/codex-delivery/harbor-live/durable-smoke-v2-summary.json`

## Boundaries

- The task content is simple and synthetic; all-pass reward does not indicate
  benchmark quality or long-horizon coding ability.
- SQLite coordinates one host. It is not a distributed broker or cross-node
  leader election system.
- A durable job can be recovered, but arbitrary external side effects are not
  magically exactly-once.
- The custom Harbor environment supports Linux single-container tasks. Tasks
  that require Compose, allowlist networking, Windows containers, GPU devices,
  or Harbor's full Docker provider remain outside this adapter's scope.
