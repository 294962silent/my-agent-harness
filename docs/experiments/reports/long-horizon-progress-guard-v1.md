# Long-horizon deadline/progress guard v1

Date: 2026-11-15
Status: offline implementation and deterministic component ablation complete.

## Scope

This experiment evaluates a task-agnostic guard for long-running coding work. It
does not call a model, external API, or Docker, and it does not make a claim about
real-model task success. The runtime files covered by `public_pilot` plan hashes
were not modified.

The guard accepts a total time budget, a warning ratio, and a maximum consecutive
no-progress count. Tool completion events use the interface:

```python
register_tool(name, args, ok, result)
```

Events may declare `progress=True` and `final_delivery=True`. A configured
delivery predicate can replace that generic convention. The guard returns a
`decision` plus `evidence`. It injects at most one structured steering message:

1. persist the requested final deliverable;
2. validate the full repository against that exact artifact.

Steering is injected when the deadline ratio reaches its threshold without a
delivery signal, or when consecutive no-progress events exceed their limit.
Early final delivery suppresses the deadline warning. An exhausted total budget
returns `stop=True` instead of injecting. The guard never writes the candidate
answer or replaces the model.

## Deterministic ablation

Configuration:

- fake-clock total budget: 10.0 seconds;
- warning ratio: 0.7;
- ablation no-progress limit: 8;
- repeats: 20.

The baseline repeatedly compiles an unchanged local diagnostic file. It never
writes the required final artifact and times out after simulated time 10.5.

The guarded run performs the same local checks, receives one steering decision at
simulated time 7.6, writes `final_artifact.py`, and then runs host subprocesses:
`python -m py_compile` and `python -m unittest discover`. Delivery precedes both
validation commands.

| Condition | Passed | Failure classes |
| --- | ---: | --- |
| baseline | 0 / 20 | `timeout_before_final_delivery`: 20 |
| guarded | 20 / 20 | none |

The 20 runs produced one distinct deterministic signature. In the recorded run,
baseline performed 7 local checks and timed out. Guarded performed 5 local checks,
injected once, and returned `py_compile=0` and `unittest=0`.

## Verification

```text
uv run pytest tests/experiments/test_progress_guard_ablation.py -q
7 passed

uv run ruff check experiments/long_horizon_code/progress_guard_ablation.py tests/experiments/test_progress_guard_ablation.py
All checks passed!

uv run python -m experiments.long_horizon_code.progress_guard_ablation --repeat 20 --compact
exit code 0; baseline 0/20; enhanced 20/20
```

## Harbor integration sketch

The smallest integration is to construct one guard when a task starts, forward
completed tool events to it, and apply only its returned decisions:

```python
guard = ProgressGuard(total_seconds=remaining_seconds, warning_ratio=0.7,
                      max_consecutive_no_progress=8, clock=time.monotonic)
decision = guard.register_tool(name, safe_args, ok, {
    "progress": evidence_changed,
    "final_delivery": artifact_persisted,
})
if decision["inject"]:
    inject_once(decision["steering"])
if decision["stop"]:
    halt_for_deadline()
```

The host must compute `progress` and `final_delivery` from generic execution
evidence, not task names. Tool arguments written to evidence should use the
existing redaction policy.

## Limits

- The clock and repository work are scripted; no real model behavior was tested.
- The host `py_compile` and `unittest` checks validate this ablation's artifact,
  not whether a model follows steering.
- Integration timing, token overhead, and steering-policy quality remain to be
  measured in the real Harbor path.
