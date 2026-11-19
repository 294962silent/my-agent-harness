# Long-Horizon Quality v24 Recovery Gate

## Result

**Provider gate failed on the first of three required requests. React and MEA
end-to-end canaries were not started.**

The test used the production credential manager and the same provider contract
as the Harbor experiments:

- Endpoint: `https://opencode.ai/inference/go/openai/v1`
- Model: `deepseek-v4.1-flash`
- OpenAI client: `3.13.0`
- HTTPX client: `0.28.1`
- Client timeout: 90 seconds
- Additional absolute timeout: 300 seconds
- Retries: 0
- Scoring: disabled
- Campaign run ID: none

## Attempt 1

| Field | Value |
|---|---|
| Result | `APITimeoutError` |
| Message | `Request timed out.` |
| Start | `2026-11-19T14:26:54.773248+00:00` |
| Finish | `2026-11-19T14:28:25.278834+00:00` |
| Latency | 90.505619 seconds |
| Response | unavailable |
| Usage | unavailable |

The failure stopped the gate. Attempts 2 and 3 were not sent because the
pre-registered requirement is for three consecutive successful requests.

## Not run

- React non-scoring end-to-end canary
- MEA non-scoring end-to-end canary
- Official verifier run
- Langfuse campaign reconciliation for these canaries
- v24 paid or descriptive cohort
- Any v24 run-ID allocation

This report records a provider recovery failure, not a model or benchmark
failure.

## Transport isolation

A subsequent no-credential network check separated the provider from the local
transport:

- The Windows process reached `https://opencode.ai/` through its configured
  local proxy and received HTTP 200.
- The WSL process timed out on direct HTTPS.
- WSL reached TCP port 17897 but HTTPS proxy requests through that relay timed
  out.
- A Docker bridge relay reached the Windows proxy from inside the container, but
  the WSL host timed out when using the relay and the published container port.

This points to the WSL-to-host data path or proxy relay, not to evidence that the
model made an incorrect answer. The exact provider-side HTTP status remains
unavailable because the production WSL client never received a response.

## Evidence

- `provider-connectivity.json`: bounded, metadata-only request record with no
  credential or raw provider response.
- `SHA256SUMS`: integrity manifest for this recovery attempt.

## Next recovery action

Repair or restart the WSL networking path, confirm the same scoped HTTPS proxy
from inside WSL, and verify that the Windows proxy can carry the provider
endpoint without changing the endpoint, model, credential manager, or native
client. Then restart the complete three-request gate from attempt 1. A failed or
partially completed gate must not be continued into the Agent canaries.
