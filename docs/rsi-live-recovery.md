# Exploratory policy recovery r1

This recovery is registered **after observing a collection failure and before
the first test trial**. It is an exploratory amendment, not a claim that the
original complete holdout protocol passed. It makes no provider calls and never
changes the frozen execution source, existing scores, reports, trial records,
partial dataset, historical ledger or original registry.

The narrow eligibility gate requires exactly the five planned collection
trials, all provider requests settled with no pending/unknown/held usage, zero
test trials, the frozen source and task materials, one original optimizer-start
marker, and no research policy, optimizer-finished marker or original SDK search
directory. Runtime reports are bound through final-evaluation hashes to durable
trial terminals. Original files and journal prefix are hashed. Any discrepancy
stops recovery.

Three train tasks must all have valid replayable traces. Exactly one of the two
holdout tasks is replayable; HumanEval/32 must retain its original quota failure,
missing trace/final result and known usage. The full five-task coverage manifest
keeps that negative result and the complete request/token denominator. Currency
and cost remain null. Recovery does not replace an absent score with zero or
synthesize a baseline-only trace.

A **new separate directory** contains an explicitly available-tree projection:
all three train traces and the one available holdout trace. The pinned actual
Dream SDK runs once with the original four candidates and action budgets
`(1, 2, 4)`, `dry_run=True` and its own new private registry. Those budgets count
recorded scored-tree node actions, not LLM requests or real tokens. This is
recorded-score replay, not new branch execution or independent model testing.

The audited selector uses only train rows and prefers baseline on a tie, then
narrower/shallower parameters. Recovery independently repeats that ordering from
the complete train-score inventory and requires the same selection. The one
available holdout is necessary for the existing SDK's interface and its raw
score is diagnostic only; it does not influence candidate sorting, tie breaking
or publication admission.

Every recovered policy has:

- `publication_status=validation_incomplete_rejected_for_publication`.
- `validation_gain=null` and `incumbent_gain=null`.
- Holdout coverage explicitly one of two, with HumanEval/32 unavailable.
- `promoted=false`, and the original synthetic/observed provenance kind retained.

If the SDK selects parameters, freeze that single train-selected candidate.
If the SDK selects baseline, preserve its entire raw selection, including null
parameters and the SDK policy hash. Online C then uses the separately registered
`online_reference_fallback` of width 2/refinement 1, with its own effective
FrozenPolicy hash. This is **not asserted equivalent to the SDK default**.
`C_condition=reference_fallback_not_trained_intervention`: C and B have the same
online parameters, so an independent learned RSI contribution is not identifiable.
Neither improvement nor “zero learning improvement” is established by that tie.

Recovery registration is appended under the existing journal lock before the
SDK search. A competing registration or later SDK failure cannot trigger another
search under a different output path. The script, input manifest, full coverage,
projection, SDK source fingerprint and output files are sealed. An explicit
prepared operator event precedes exclusive policy creation and a unique
optimizer-finished digest. The original runner can then consume the frozen
policy and skip the five already completed collection slots, continuing only
the 48 unstarted test slots. It does not recollect train/holdout.

If the final commit is interrupted, only `complete-commit` may finish the same
sealed candidate. It performs no SDK search and refuses changed source, inputs,
SDK, candidate files, original journal prefix, new trial activity or duplicate
completion. An unsealed SDK failure requires manual review; the tool does not
repair or discard its evidence. Hashes are trusted-host integrity evidence, not
proof against a malicious host, remote execution attestation or an OS sandbox.

From the repository root, using explicit approved paths:

```powershell
.venv/Scripts/python.exe -m experiments.context_rsi.recover_research_policy preflight --campaign CAMPAIGN
.venv/Scripts/python.exe -m experiments.context_rsi.recover_research_policy recover --campaign CAMPAIGN --output NEW_RECOVERY_DIRECTORY
# Only for an interrupted, already prepared commit; never repeats optimization:
.venv/Scripts/python.exe -m experiments.context_rsi.recover_research_policy complete-commit --campaign CAMPAIGN --output SAME_RECOVERY_DIRECTORY
```

The regression fixtures are synthetic and make zero provider calls. They cover
full-denominator preservation, missing train/holdout rejection, immutable raw
artifacts, concurrent registration, interrupted commits, no re-search after a
failure, plus actual offline SDK selection and baseline fallback on three train
trees and one available holdout tree. They do not establish model quality.
