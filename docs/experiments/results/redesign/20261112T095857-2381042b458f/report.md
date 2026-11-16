# Offline harness evaluation

Synthetic fixtures exercise actual production paths. Paired observations are not evidence of real-model quality.
Passing means the expected engineering contract was observed; an off arm may intentionally expose a safety failure.
A comparison wrapper is explicitly identified when there is no production feature switch.
Contract cases have one arm. Repeats are timing samples, not additional unique tasks.

Unique cases: 12; raw records: 100; assertion failures: 0.

## context / artifact_roundtrip / off (ablation)

Passed 5/5; median 0.003913s; p95 0.008211s.
Metrics: {"artifact_disk_bytes": {"maximum": 0, "mean": 0, "minimum": 0}, "artifact_readback_supported": {"maximum": 0, "mean": 0, "minimum": 0}, "evidence_recoverable": {"maximum": 1, "mean": 1, "minimum": 1}, "handles": {"maximum": 0, "mean": 0, "minimum": 0}, "source_bytes": {"maximum": 120019, "mean": 120019, "minimum": 120019}, "view_bytes": {"maximum": 120019, "mean": 120019, "minimum": 120019}, "view_contains_full_source": {"maximum": 1, "mean": 1, "minimum": 1}}

## context / artifact_roundtrip / on (ablation)

Passed 5/5; median 0.043170s; p95 0.049984s.
Metrics: {"artifact_disk_bytes": {"maximum": 120361, "mean": 120361, "minimum": 120361}, "artifact_readback_matches_source": {"maximum": 1, "mean": 1, "minimum": 1}, "artifact_readback_supported": {"maximum": 1, "mean": 1, "minimum": 1}, "evidence_recoverable": {"maximum": 1, "mean": 1, "minimum": 1}, "handles": {"maximum": 1, "mean": 1, "minimum": 1}, "source_bytes": {"maximum": 120019, "mean": 120019, "minimum": 120019}, "view_bytes": {"maximum": 7041, "mean": 7041, "minimum": 7041}, "view_contains_full_source": {"maximum": 0, "mean": 0, "minimum": 0}}

## context / summary_source_binding / contract (contract)

Passed 5/5; median 0.002518s; p95 0.003649s.
Metrics: {"changed_source_summary_leaks": {"maximum": 0, "mean": 0, "minimum": 0}, "summarizer_calls": {"maximum": 1, "mean": 1, "minimum": 1}, "unchanged_source_summary_reused": {"maximum": 1, "mean": 1, "minimum": 1}}

## memory / labeled_retrieval / off (ablation)

Passed 5/5; median 0.033769s; p95 0.036014s.
Metrics: {"hits": {"maximum": 0, "mean": 0, "minimum": 0}, "labeled_queries": {"maximum": 2, "mean": 2, "minimum": 2}, "recall": {"maximum": 0.0, "mean": 0.0, "minimum": 0.0}, "stale_hits": {"maximum": 0, "mean": 0, "minimum": 0}}

## memory / labeled_retrieval / on (ablation)

Passed 5/5; median 0.035606s; p95 0.040100s.
Metrics: {"hits": {"maximum": 2, "mean": 2, "minimum": 2}, "labeled_queries": {"maximum": 2, "mean": 2, "minimum": 2}, "recall": {"maximum": 1.0, "mean": 1.0, "minimum": 1.0}, "stale_hits": {"maximum": 0, "mean": 0, "minimum": 0}}

## model / stream_cancel / contract (contract)

Passed 5/5; median 0.006189s; p95 0.018961s.
Metrics: {"cancellation_propagated": {"maximum": 1, "mean": 1, "minimum": 1}, "provider_finalized": {"maximum": 1, "mean": 1, "minimum": 1}}

## model / stream_close / contract (contract)

Passed 5/5; median 0.005035s; p95 0.006046s.
Metrics: {"provider_finalized": {"maximum": 1, "mean": 1, "minimum": 1}, "terminal_records": {"maximum": 1, "mean": 1, "minimum": 1}}

## observation / auxiliary_coverage / off (ablation)

Passed 5/5; median 0.010203s; p95 0.050223s.
Metrics: {"actual_calls": {"maximum": 2, "mean": 2, "minimum": 2}, "coverage": {"maximum": 0.0, "mean": 0.0, "minimum": 0.0}, "linked_auxiliary": {"maximum": 0, "mean": 0, "minimum": 0}, "matched_ends": {"maximum": 0, "mean": 0, "minimum": 0}, "observed_calls": {"maximum": 0, "mean": 0, "minimum": 0}, "privacy_violations": {"maximum": 0, "mean": 0, "minimum": 0}}

## observation / auxiliary_coverage / on (ablation)

Passed 5/5; median 0.009373s; p95 0.011317s.
Metrics: {"actual_calls": {"maximum": 2, "mean": 2, "minimum": 2}, "coverage": {"maximum": 1.0, "mean": 1.0, "minimum": 1.0}, "linked_auxiliary": {"maximum": 1, "mean": 1, "minimum": 1}, "matched_ends": {"maximum": 2, "mean": 2, "minimum": 2}, "observed_calls": {"maximum": 2, "mean": 2, "minimum": 2}, "privacy_violations": {"maximum": 0, "mean": 0, "minimum": 0}}

## permission / unauthorized_write / off (ablation)

Passed 5/5; median 0.026613s; p95 0.034719s.
Metrics: {"unauthorized_side_effects": {"maximum": 1, "mean": 1, "minimum": 1}}

## permission / unauthorized_write / on (ablation)

Passed 5/5; median 0.031489s; p95 0.592409s.
Metrics: {"unauthorized_side_effects": {"maximum": 0, "mean": 0, "minimum": 0}}

## runtime / single_owner / contract (contract)

Passed 5/5; median 0.011687s; p95 0.012704s.
Metrics: {"concurrent_prompt_rejected": {"maximum": 1, "mean": 1, "minimum": 1}, "owner_released": {"maximum": 1, "mean": 1, "minimum": 1}}

## runtime / turn_budget / off (ablation)

Passed 5/5; median 0.048270s; p95 0.053242s.
Metrics: {"budget_exhausted": {"maximum": 0, "mean": 0, "minimum": 0}, "model_calls": {"maximum": 5, "mean": 5, "minimum": 5}}

## runtime / turn_budget / on (ablation)

Passed 5/5; median 0.029387s; p95 0.031863s.
Metrics: {"budget_exhausted": {"maximum": 1, "mean": 1, "minimum": 1}, "model_calls": {"maximum": 2, "mean": 2, "minimum": 2}}

## session / branch_restore / off (comparison)

Passed 5/5; median 0.023976s; p95 0.026551s.
Metrics: {"branch_leaks": {"maximum": 1, "mean": 1, "minimum": 1}, "messages": {"maximum": 3, "mean": 3, "minimum": 3}}

## session / branch_restore / on (comparison)

Passed 5/5; median 0.023760s; p95 0.025646s.
Metrics: {"branch_leaks": {"maximum": 0, "mean": 0, "minimum": 0}, "messages": {"maximum": 2, "mean": 2, "minimum": 2}}

## tools / safe_parallel / off (ablation)

Passed 5/5; median 0.048602s; p95 0.054242s.
Metrics: {"completed": {"maximum": 3, "mean": 3, "minimum": 3}, "peak_concurrency": {"maximum": 1, "mean": 1, "minimum": 1}}

## tools / safe_parallel / on (ablation)

Passed 5/5; median 0.015935s; p95 0.157499s.
Metrics: {"completed": {"maximum": 3, "mean": 3, "minimum": 3}, "peak_concurrency": {"maximum": 3, "mean": 3, "minimum": 3}}

## tools / write_read_dependency / off (comparison)

Passed 5/5; median 0.014878s; p95 0.016116s.
Metrics: {"dependency_violations": {"maximum": 1, "mean": 1, "minimum": 1}}

## tools / write_read_dependency / on (comparison)

Passed 5/5; median 0.017076s; p95 0.017537s.
Metrics: {"dependency_violations": {"maximum": 0, "mean": 0, "minimum": 0}}

Tokens, cache usage and real-model quality: unavailable (null).
Durations include local fixture setup and filesystem work. Small samples are not throughput benchmarks.
