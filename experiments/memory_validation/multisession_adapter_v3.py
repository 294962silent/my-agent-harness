"""V3 native extraction adapter with an explicit 32K output ceiling."""

from __future__ import annotations

from experiments.memory_validation.multisession_adapter import GuardedMem0LLM


V3_EXTRACTION_OUTPUT_CAP = 32_768


class GuardedMem0LLMV3(GuardedMem0LLM):
    """Keep the v2 adapter contract while raising only its output ceiling."""

    def __init__(
        self,
        owner,
        ledger,
        *,
        run_id,
        task_id,
        audit_path,
        max_output_tokens=V3_EXTRACTION_OUTPUT_CAP,
        request_timeout=600,
        after_settle=None,
    ):
        if max_output_tokens != V3_EXTRACTION_OUTPUT_CAP:
            raise ValueError("v3 extraction output cap differs from the frozen recovery")
        # The preserved v2 adapter intentionally rejects this cap. Initialize its
        # unchanged state machine with the old value, then apply the v3 call limit.
        super().__init__(
            owner,
            ledger,
            run_id=run_id,
            task_id=task_id,
            audit_path=audit_path,
            max_output_tokens=8192,
            request_timeout=request_timeout,
            after_settle=after_settle,
        )
        self.max_output_tokens = max_output_tokens
