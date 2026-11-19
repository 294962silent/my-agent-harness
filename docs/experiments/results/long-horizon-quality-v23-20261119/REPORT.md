# Long-Horizon Quality Pilot v23

## Status

**Stopped after two canary slots because the provider became unavailable. No
official task was scored.**

Campaign: `lhq-tb21-pilot-20261119-v23`

Frozen source: `9a4918a`

Plan SHA-256: `adb1ef8c855d032aa550543126be9f453d821cb3a774ebcdc30bd2c230b7d987`

All 18 run IDs were newly generated; none overlapped v20, v21, or v22. The
campaign remains a descriptive protocol record and cannot support pass-rate,
significance, React-versus-MEA, cache-quality, or long-horizon quality claims.

## Frozen runtime bootstrap

The preflight used one content-addressed verifier bootstrap for all three
Terminal-Bench images:

- Ubuntu APT mirror: `http://mirrors.tuna.tsinghua.edu.cn/ubuntu`
- APT HTTP proxy: `http://172.19.80.1:17897`
- uv: `0.9.5`
- Python: `3.13.9`
- pytest: `8.4.1`
- pytest-json-ctrf: `0.3.5`
- Provider request timeout: 90 seconds
- Bootstrap manifest SHA-256:
  `f5848e3d48568859e9f2762d849d0ee0825815f2ee85be3053f0aa614a667418`
- Bootstrap tree SHA-256:
  `b416ca1c07c8b07602dc830887c0f824bac7eae9e657c29d4c6608c11423daff`

`readiness/preflight.json` records successful bootstrap execution in all three
images. Langfuse environment presence and endpoint checks passed, the credential
manager resolved an endpoint-matching credential, and the scoped proxy was
reachable.

## Canary result

The first two slots were `fix-git/React/t0` and `fix-git/React/t1`. Both failed
their first provider request with `BudgetStop: model request failed; reservation
retained`. Their ledgers contain one `error` request each, no usage, and unknown
cost. The official verifier never ran, so neither slot has a reward.

A separate non-campaign diagnostic used the same production credential manager,
endpoint, model, and native provider client. After the frozen 90-second timeout
it returned:

```text
credential_source=production_auth_manager
error_type=APITimeoutError
error_message=Request timed out.
```

This isolates the stop to the provider request boundary. It does not establish a
specific upstream status because the client returned only a timeout.

## Stop decision

The pre-registered canary rule was applied: two corresponding failures followed by
a reproduced native timeout stop the cohort. The remaining 16 slots were not
started, and the two consumed run IDs were not reused.

Known results:

- 2 run IDs consumed.
- 2 provider requests recorded as errors.
- 0 provider requests with usage known.
- 0 official rewards.
- 16 slots not started.
- Langfuse metadata-only readback matched both attempted sessions: 2 remote
  observations for 2 local requests; no prompt or answer bodies were read back.

## Evidence

- `preflight.json`: complete, zero-model verifier/bootstrap receipt.
- `plan.json`, `journal.json`, and `ledgers/`: frozen identities and failed
  attempts with unknown usage retained.
- `summary.json`: conservative extraction and category counts.
- `langfuse-readback.json`: remote session/observation metadata.
- `SHA256SUMS`: integrity manifest for the archive.

The unmodified private campaign tree remains under
`.git/codex-delivery/long-horizon-quality/pilot-v23`.

## v24 recovery gate

A future cohort may start only after:

1. The `BudgetStop + APITimeoutError + unknown usage` chain is diagnosed.
2. The identical endpoint, model, credential manager, and client return valid
   responses for three consecutive non-scoring requests within hard limits.
3. Unknown usage remains unavailable and is never treated as zero cost.
4. React and MEA each complete an end-to-end non-scoring canary with matching
   Langfuse reconciliation and no BudgetStop.
5. Code, verifier bootstrap, images, limits, and provider configuration are
   frozen again before assigning 18 fresh v24 run IDs.
