# Long-Horizon Quality v24 Recovery Gate

## Status

Preregistered, not started.

No v24 run ID may be created until every gate below passes. v22 and v23 remain
sealed as protocol and infrastructure records and are not reused.

## Required provider recovery

1. Diagnose the observed chain:
   `BudgetStop -> model request failed -> APITimeoutError -> usage unknown`.
2. Use the production credential manager, endpoint, model, request schema, and
   native provider client that v24 will use.
3. Complete three consecutive non-scoring requests within the hard limits:
   no more than 90 seconds without a response event and no more than 300
   seconds per request.
4. Record response validity and latency, but do not treat the diagnostic calls
   as benchmark outcomes or add their usage to a quality denominator.

## Required accounting behavior

- A failed or timed-out request keeps unknown token and cost fields unavailable.
- Unknown usage is never converted to zero.
- Any unsettled reservation blocks the affected campaign.
- Retry is zero unless a separately frozen v24 protocol explicitly authorizes a
  new attempt with a new identity.

## Required end-to-end canaries

Before assigning the 18 v24 slots:

1. React must complete one non-scoring end-to-end canary with a normal first
   provider request, successful verifier bootstrap, and no `BudgetStop`.
2. MEA must complete the same canary through Manager, Executor, Auditor,
   Manager terminal decision, and the official verifier.
3. Langfuse must reconcile the local request count and available usage metadata
   for each canary.
4. Canary results are not included in v24 quality or cost comparisons.

## Frozen environment

After all gates pass, refreeze:

- source commit and file hashes;
- image tags/digests and task revision;
- provider endpoint, model, SDK/client version, and request timeout;
- verifier bootstrap tree hash;
- context, request, spend, and wall-clock limits;
- all 18 newly generated v24 run IDs;
- claim ceiling `descriptive_only`.

If the first v24 paid slot reproduces the provider timeout chain, stop the entire
cohort. Do not replace or selectively retry individual slots.
