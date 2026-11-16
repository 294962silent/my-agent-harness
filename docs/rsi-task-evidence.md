# Public task evidence for RSI datasets

An optional public-content contract distinguishes different tasks that happen
to produce identical recorded score curves. The prior curve-only rule rejected
distinct HumanEval inputs with identical all-one curves. The rule is retained
for legacy and mixed evidence; no scores, search objective or task selection
are changed to pass the loader.

The host calls `make_task_evidence(public_input=..., visible_spec=...,
input_file="solution.py")` from `dream_rsi_contract`. The result contains exactly
schema version 1, UTF-8 public input and its SHA256, UTF-8 visible specification
and its SHA256, and a plain relative input filename. Both content fields must be
nonempty and at most 128,000 UTF-8 bytes. IDs, absolute paths, time labels,
canonical solutions and final test contents are not evidence fields.

`RsiRuntime(..., task_evidence=evidence)` validates and copies the evidence into
immutable bytes. Before execution and each branch attempt it checks the frozen
baseline inventory and exact input bytes. This initial evidence interface
supports a single UTF-8 public input file; a multi-file source requires a future
explicit content contract. External mutation of the caller's dictionary cannot
change the frozen evidence. The trace carries the public evidence and digest;
the runtime report, dataset entry and copied trace must agree on the digest.
Absent evidence keeps the earlier runtime/dataset behavior.

The loader recomputes all public-content hashes and the proof digest. It
separates two identities:

- The existing semantic score identity still ignores node labels/paths and
  branch numbering. This remains useful for conservative legacy overlap checks.
- Public input identity hashes the exact input content, excluding task ID,
  input filename and visible-spec relabeling. The same input cannot cross
  train/holdout even after changing IDs, paths, scores or visible checks.

Two entries whose public inputs are both verified and different may share a
score curve across splits. If either entry lacks evidence, an equal curve
remains a rejection. Same task IDs across splits remain a rejection. A forged
content hash, missing binding or changed report/trace digest is a rejection.
Existing schema-v1 entries without the optional digest remain compatible.

This is **trusted-host, recomputable provenance**, not cryptographic proof that
an honest host executed a remote service or the declared checker. The pure
loader checks contents and hashes, not whether the visible specification was
actually executed or whether input text came from a particular official
dataset revision. The experiment host's verified task adapter supplies that
origin binding and must recheck task materials. Exact bytes are compared;
arbitrary rewriting of a prompt is outside the origin guarantee. Neither
`observed` nor valid public evidence alone proves model quality.

For HumanEval, the host constructs evidence only after `PreparedTask.verify()`
from the original prompt-only `solution.py` and public original-entry-point
doctest specification. The private task JSON, canonical solution and official
final tests must not be copied into this evidence. Independent final checks
remain host-only, post-selection results and do not enter optimizer feedback.

The regression `tests/coding/test_rsi_task_evidence.py` includes two distinct
public HumanEval prompts with deliberately synthetic equal scores, same-input
renaming/score-change rejections, legacy/mixed rules, hash drift rejection and
real runtime/export binding. These are zero-provider protocol contracts, not
observed model outcomes. The existing anti-renaming and replay tests remain.
