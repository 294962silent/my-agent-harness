# Long-Horizon Quality v24 Status

Updated: 2026-11-19

## Current gate

Status: **blocked at provider connectivity**

- Provider recovery requests passed: **0 / 3**
- First request result: `APITimeoutError`
- Error message: `Request timed out.`
- Observed latency: 90.505619 seconds
- Response and usage: unavailable
- React canary: not started
- MEA canary: not started
- v24 campaign run IDs: none allocated
- Quality denominator: empty

The partial gate was stopped after the first failure. It must restart from
attempt 1 after the provider path is fixed; attempts 2 and 3 must not be treated
as continuations of this failed run.

Transport isolation:

- Windows reached `https://opencode.ai/` through its local proxy (HTTP 200).
- WSL direct HTTPS timed out.
- WSL HTTPS through the Windows relay and a container-published relay also
  timed out at the data-transfer step, despite TCP connectivity.
- Therefore the current blocking layer is the WSL networking/proxy path. The
  provider endpoint must be retested after that transport is restored.

## Sealed evidence

- [v22 report](results/long-horizon-quality-v22-20261119/REPORT.md): mixed
  verifier environment; 10/18 attempted; no quality claim.
- [v23 report](results/long-horizon-quality-v23-20261119/REPORT.md): unified
  bootstrap; stopped at 2/18 after provider timeout.
- [v24 recovery report](results/long-horizon-quality-v24-recovery-20261119/REPORT.md):
  provider gate failed on request 1/3.
- [v24 recovery gate](../plans/long-horizon-quality-v24-recovery-gate.md):
  frozen conditions required before any cohort starts.
- [experiment log](HARNESS_EXPERIMENT_LOG.md): append-only implementation and
  failure history.

## Next executable gate

After the WSL networking path is restored, write a new result path rather
than overwriting the failed evidence:

```bash
PYTHONPATH=src:. python -m experiments.long_horizon_quality.provider_recovery_probe \
  --attempts 3 \
  --output docs/experiments/results/<new-recovery-dir>/provider-connectivity.json
```

Only if all three requests pass may the non-scoring React and MEA canaries run.
No v24 cohort IDs are created before both canaries pass.
