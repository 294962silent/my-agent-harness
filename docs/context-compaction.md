# Layered local context projection

`ContextManager` keeps the existing `baseline` mode by default. `layered` is an
explicit experiment option, not a claim that five provider-native cache features
were reproduced. It does not send `cache_edits` or another cache mutation API.

```python
from my_agent_core.context import ContextManager
from my_agent_core.context_compaction import CompactionPolicy

policy = CompactionPolicy(
    mode="layered",
    model_context_window=resolved_model_context_window,
    output_reserve=configured_output_tokens,
    safety_margin=1024,
    summary_max_tokens=1024,
    recent_tool_groups=2,
)
context = ContextManager(
    budget=configured_context_budget,
    llm=parent_budgeted_llm,
    artifact_store=owned_artifact_store,
    compaction_policy=policy,
)
view = await context.prepare(canonical_messages)
report = context.compaction_report
```

The window must come from the caller's resolved model capability, not a guessed
200k limit. If omitted, the report explicitly says `caller_budget_only`; the code
does not infer or certify a provider window. Token counts remain character/usage
anchored estimates, not exact provider tokenizer counts. Input reserves reduce
the effective threshold; an unsafe view raises `ContextBudgetExceeded` before a
normal model request. The summarizer also reserves its own output and safety
space and uses the same injected LLM with `purpose="summary"`. No bare client or
unbudgeted retry is created. A summary request that cannot fit is refused, not
silently sliced or split into additional hidden calls.

The sequence is canonical identity validation, existing branch-summary
reconstruction, artifact projection, deterministic tool micro projection, an
optional completed-prefix fold, and finally bounded LLM summarization. Large
tool bodies use the existing immutable content-addressed ArtifactStore and bounded
read/search tools. Old groups are eligible for micro projection only when all
parallel tool results are present and their success is explicit in a known status
field. Recent groups, unfinished calls, errors, unknown status and bounded
artifact reads stay visible. Disk failure preserves the source view rather than
advertising an unreadable handle. Reused tool call IDs cannot overwrite a
different content-addressed body.

Arbitrary LLM sentence deletion and precise semantic trimming are unsupported.
No claim of lossless summary semantics is made. The summary prompt requests goals,
constraints, decisions, changed files, validation, pending work and artifact
references. Omitted recoverable artifact handles are appended deterministically.
The canonical transcript and Session nodes remain unchanged.

An explicit host integration can fold a completed prefix without a new LLM call:

```python
context.register_completed_prefix(
    canonical_messages,
    cut=completed_prefix_end,  # canonical coordinates, not the projected view
    summary=host_supplied_structured_stage_summary,
)
view = await context.prepare(canonical_messages)
# Existing Agent handling persists pending_compaction through ContextSessionBridge.
```

The host owns the completion decision and summary fidelity. The fold is accepted
only for an unchanged fingerprinted prefix ending outside a tool group and only
when its projected result fits. It uses the existing prefix coverage persistence
schema; arbitrary middle-span folds are not supported. Dynamic micro content is
never stored as summary cache identity: retained snapshots and covered message
fingerprints refer to canonical messages. Branch changes invalidate mismatching
coverage. A same-input repeat does not issue another summary call.

`CompactionPolicy.capability_profile()` records configured capabilities;
`compaction_report` adds source fingerprints, input threshold, applied group count,
phase-fold status and estimated size. Message rewrites mark request-projection
reasons (`artifact_preview`, `local_tool_micro_projection`,
`local_completed_prefix_fold`, `local_context_summary`). Rewriting a prefix can
invalidate a provider cache. These diagnostics do not prove cache hits or speedup.

For A/B experiments, hold model, output limit, effective caller context budget,
artifacts/read tools, task and parent request/cost/time guards fixed. Switch only
`mode` between baseline and layered and archive both capability profiles. Baseline
retains the original compatibility path, including its original failure behavior;
layered adds explicit overflow rejection. No real API or SWE result is established
by the offline tests.

Offline verification uses real ContextManager, Session save/load and ArtifactStore
with fake summarizer responses. It covers canonical immutability, parallel tool
groups, readable handles, ID reuse, disk failure, branch restoration, stable
repeated preparation, canonical cache tails, and bounded/manual summary failure.

The initial user request and messages explicitly tagged
`metadata={"context_protected": True}` are kept verbatim in layered summaries.
This protects the source goal/constraints without claiming that an LLM can
identify every later constraint perfectly. The current user turn is retained
whole. If protected text alone cannot fit, the request is refused rather than
discarding it. For ordinary application assembly, pass the same policy through
`CodingAgent(compaction_policy=policy, ...)` to core Agent/ContextManager; the
factory owns resolution of the actual model window and its provenance.
