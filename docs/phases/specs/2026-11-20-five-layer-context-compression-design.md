# Five-Layer Context Compression Design

Status: approved design

## Goal

Implement a five-layer context projection pipeline whose observable behavior
aligns with the five Claude Code compression layers described for this project:

1. Tool Result Budget
2. Snip
3. Microcompact
4. Context Collapse
5. Auto-Compact

The canonical Session transcript remains intact. Every layer changes only the
view sent to the model.

Claude Code's internal source and exact provider-specific thresholds are not
available here. This design matches the observable layer responsibilities; it
does not claim a line-for-line reproduction.

## Non-Goals

- Do not physically delete canonical Session messages.
- Do not claim measurable KV-cache improvement without a separate experiment.
- Do not add a new LLM provider or remote service.
- Do not make Snip, Microcompact, or Context Collapse invisible in diagnostics.
- Do not change the public Agent invocation API.

## Current Gap

The current production path contains:

- per-result artifact projection for tool messages above 6,000 characters;
- a legacy disk-spill fallback when no `ArtifactStore` exists;
- an 80%-of-context-window model summary that retains a recent tail.

It does not contain active Snip, Microcompact, or independent Context Collapse
layers. The old `snip_messages` and `micro_compact` functions were removed in
commits `f8f0cf8` and `c8525ff`.

## Core Architecture

`ContextManager.prepare()` becomes the orchestrator for a pure request-projection
pipeline:

```text
canonical Session messages
  -> deterministic message identities
  -> Layer 1: Tool Result Budget
  -> estimate
  -> Layer 2: Snip
  -> estimate
  -> Layer 3: Microcompact, only in a cache-invalidated epoch
  -> estimate
  -> Layer 4: Context Collapse
  -> estimate
  -> Layer 5: Auto-Compact
  -> final bounded request view
```

Each layer runs only if the current projection remains above the trigger budget.
After a layer acts, the next layer is considered. The first successful reduction
below the trigger stops the pipeline.

The Layer 1 projection is deterministic and may run below the high-water mark.
Layers 2-5 start only after the request exceeds the configured threshold.

## Data Model

### Stable projection IDs

Each canonical message receives a deterministic projection ID:

```text
projection:<role>:<content-and-metadata-sha256>:<duplicate-ordinal>
```

The duplicate ordinal makes repeated identical messages independently
addressable. Tool messages retain their `tool_call_id` separately for pair
validation. Projection IDs are regenerated after canonical history changes and
must not be reused across an invalidated branch or Session.

### ContextProjectionPlan

The in-memory immutable plan stores:

- canonical-hash and provider/model identity;
- cache-invalidation epoch;
- selected tool-result artifact IDs;
- Snip-covered projection IDs;
- Microcompact-covered tool-call IDs;
- Context Collapse source IDs and summary;
- protected recent-tail boundary;
- current layer and pre/post token estimates;
- invalidation reason.

A plan is valid only when its canonical-prefix identity and branch identity still
match. A mismatch discards the plan and rebuilds from canonical messages.

### Persistence

Layer state is persisted append-only in a `CompactionEntry` metadata object:

```json
{
  "entry_kind": "request_projection",
  "projection_only": true,
  "provenance_version": 1,
  "source_path_id": "<current-entry>",
  "branch_id": "<current-entry>",
  "canonical_source_hash": "...",
  "projection_plan": {
    "version": 1,
    "source_count": 42,
    "source_hash": "...",
    "snip_ids": [],
    "microcompact_projection_ids": [],
    "collapse": {},
    "layers": [],
    "cache_epoch": 1,
    "budget_exceeded": true
  }
}
```

Projection-only entries:

- do not change `current_id`;
- do not update `compaction_floor`;
- are ignored by summary-cache lookup;
- are ignored by Session message folding;
- are validated against the current path before reuse.

The existing Session JSONL schema remains compatible because this information
uses the existing `CompactionEntry.metadata` field.

## Layer 1: Tool Result Budget

### Per-result budget

- Default threshold: 6,000 characters.
- Larger text results are stored in the content-addressed `ArtifactStore`.
- The request view receives a deterministic head/tail preview and artifact
  handle.
- The canonical Session keeps the complete tool result.
- Artifact save failure produces an explicit bounded `full_unavailable` preview.

### Per-turn aggregate budget

- Default threshold: 24,000 characters for all tool results following one
  assistant tool-call message.
- Group messages by assistant `tool_calls.id`.
- Preserve source order.
- First externalize every result over the per-result budget.
- If the remaining group total is still over budget, externalize the largest
  remaining results first, using source order as the tie-breaker.
- Never split a tool call from its result.
- Trusted bounded tools `artifact_read` and `artifact_search` are never wrapped
  into a second artifact.

Layer 1 does not invalidate the cache epoch when it projects a newly-completed
tool group. Once emitted, the same result must produce byte-identical previews.

## Layer 2: Snip

### Trigger

Run after Layer 1 only when the request remains above the configured trigger.
Select the oldest safe complete conversation groups until the request falls
below the trigger or no more safe groups remain.

The production orchestrator reserves the newest eligible middle group for
Context Collapse. The pure selector defaults to no reserve; this explicit
separation keeps the low-level API general while ensuring the five-layer
product pipeline does not make Layer 4 unreachable.

### Safety rules

Never snip:

- the system prompt;
- the initial task/constraints group;
- the current user request;
- the open tool-call group;
- the messages represented by the configured recent-token tail;
- messages referenced by the active Auto-Compact summary;
- messages outside the current branch.

An eligible group is one user turn and all following assistant/tool messages up
to the next user message. A group containing an incomplete call/result pair is
not eligible.

Snip deletions are recorded by projection ID in the frozen plan. The first
activation establishes a new request-view epoch; subsequent append-only turns
reuse the same deletions.

## Layer 3: Microcompact

### Cache rule

Microcompact runs only when:

- the request is still above the trigger after Snip; and
- the current cache epoch is explicitly invalidated.

Explicit invalidation events are:

- provider or model change;
- branch, Session, or compaction-floor change;
- Auto-Compact summary replacement;
- provider-reported cache miss.

Time-based TTL is not used.

### Body removal

Select old tool results outside the protected recent tail. Ignore trusted bounded
artifact results and ignore bodies shorter than the configured minimum.

Replace selected bodies with:

```text
[microcompacted tool result; tool_call_id=<id>; use the canonical transcript
for audit]
```

Keep:

- the assistant tool-call message;
- the tool result message;
- `tool_call_id`;
- source order;
- saved artifact references when present.

No new model call is made. The selected tool-call IDs are frozen in the current
plan until the next explicit cache invalidation.

## Layer 4: Context Collapse

### Scope

Run after Snip and Microcompact when the request remains above the trigger.
Select a contiguous middle-history region bounded by:

- protected task head on the left;
- protected recent tail on the right;
- complete user/tool groups at both ends.

Context Collapse requires a nonempty middle region. If no safe region exists, the
layer is skipped.

### Projection

Render:

```text
[system]
[protected task head]
[collapsed-context summary]
[recent tail]
```

The summary prompt asks only for the selected middle region. It must preserve:

- decisions and rationale;
- failed attempts and why they failed;
- evidence references;
- unresolved questions;
- files and commands changed or inspected;
- next actions implied by that region.

The summary is persisted in the projection plan and in a projection-only
CompactionEntry. The original middle messages remain canonical.

Model-call failure, timeout, empty output, or malformed summary skips the layer
and allows Auto-Compact to proceed.

## Layer 5: Auto-Compact

Auto-Compact remains the final fallback. It runs only when Layers 1-4 leave the
request above the trigger.

The existing structured handoff summary is retained:

- Goal
- Constraints and Preferences
- Progress
- Key Decisions
- Next Steps
- Critical Context

It summarizes the oldest prefix, retains the configured recent tail, records
usage/model metadata, and persists the existing summary CompactionEntry with
Floor protection.

`reserve_tokens` participates in the effective context budget. If all five
layers finish above the effective budget, the pipeline returns the smallest safe
view and records:

```json
{
  "status": "budget_exceeded",
  "layers_applied": []
}
```

It does not pretend compaction succeeded.

## Configuration

Wire the existing `CompactionSettings` into the RPC-created `CodingAgent`.
Extend it with defaults:

```text
enabled: true
reserveTokens: 16384
keepRecentTokens: 20000
toolResultMaxChars: 6000
toolRoundMaxChars: 24000
contextTriggerRatio: 0.80
microcompactMinChars: 200
```

Validation rules:

- values are positive and bounded;
- `keepRecentTokens < effective context budget`;
- `toolRoundMaxChars >= toolResultMaxChars`;
- `contextTriggerRatio` is in `(0, 1]`.

`enabled=false` returns the integrity-cleaned canonical request view without
applying any of the five layers.

## Failure Behavior

- Artifact save failure: bounded preview; no fake handle.
- Snip with no safe group: no-op.
- Microcompact without invalidation: no-op.
- Collapse model failure: preserve original middle and continue to Auto-Compact.
- Auto-Compact failure: preserve the prior safe view.
- Projection-plan identity mismatch: discard stale plan and rebuild.
- Budget still exceeded after all layers: return the smallest safe view and mark
  the result `budget_exceeded`.
- All failures are recorded with layer names; no exception body or credentials
  enter observability output.

## Observability

Extend `CompactionInfo` and `ContextCompacted` without breaking existing callers:

- `layers_applied`
- `layer_metrics`
- `budget_before`
- `budget_after`
- `cache_epoch`
- `status`

Request projection reasons use:

```text
layer1:tool_budget
layer2:snip
layer3:microcompact
layer4:collapse
layer5:auto_compact
```

No raw prompt or tool body is added to telemetry.

## Testing

### Layer tests

- Per-result budget replaces a large single result and retains read-back.
- A same-turn group over total budget externalizes deterministic largest items.
- Snip removes only complete old groups and keeps protected tail/system.
- Microcompact is a no-op without invalidation and preserves tool structure after
  invalidation.
- Context Collapse keeps protected head and tail and changes only the middle
  region.
- Auto-Compact runs only after the first four layers remain above budget.

### State tests

- Repeated `prepare()` calls over unchanged history return byte-identical views.
- Appending messages preserves the frozen projection prefix.
- Branch switch, Session switch, model switch, and summary replacement invalidate
  the cache epoch.
- Resume restores and validates a projection plan.
- Stale or unrelated-branch plans are rejected.

### Integration tests

- RPC-created `CodingAgent` receives all compaction settings.
- `enabled=false` disables all five layers.
- `reserve_tokens` lowers the effective trigger budget.
- Artifact read-back remains available after Microcompact or Collapse.
- Tool-history repair runs before projection and cannot create orphan pairs.

### Regression tests

- Existing `ContextManager` summary, cache reuse, Floor, branch, artifact,
  provider-projection, and RPC compaction tests remain green.
- Ruff and `git diff --check` pass.
- No real-model or benchmark claim is made by unit tests.

## Rollout

1. Introduce pure layer functions and deterministic IDs behind default-on
   settings.
2. Route RPC compaction settings into `CodingAgent`.
3. Add projection-plan persistence and resume validation.
4. Update documentation to replace historical L1/L2 claims with the actual
   five-layer status.
5. Run targeted tests, then the full context and coding suites.

The implementation is complete only when the source, tests, experiment log, and
operator documentation all describe the same five active layers.
