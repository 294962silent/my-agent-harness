from __future__ import annotations

from experiments.memory_validation.confirmation_judge_sensitivity_v1 import (
    ACCOUNTING_UNITS,
    EXPECTED_MODEL_CALLS,
    JUDGE_OUTPUT_CAP,
    MAX_REQUESTS,
    PROTOCOL,
    freeze,
)
from experiments.memory_validation.followup_common import seal
from experiments.memory_validation.multisession_protocol import sha


def _case(question_id, arm):
    messages = [
        {"role": "system", "content": "judge"},
        {"role": "user", "content": question_id + ":" + arm},
    ]
    return {
        "question_id": question_id,
        "arm": arm,
        "category": 1,
        "run_id": "confirmation-sensitivity.v1.judge." + question_id + "." + arm,
        "messages": messages,
        "messages_sha256": sha(messages),
    }


def test_confirmation_sensitivity_freezes_exact_three_length_cases(tmp_path):
    output = tmp_path / "study"
    output.mkdir()
    cases = [
        _case("q_1", "raw_lexical"),
        _case("q_2", "raw_hybrid_rrf"),
        _case("q_3", "raw_hybrid_rrf"),
    ]
    seal(
        output,
        "plan.json",
        {
            "protocol": PROTOCOL,
            "campaign_id": "test-campaign",
            "ledger_path": str(tmp_path / "ledger.json"),
            "cases": cases,
        },
    )
    receipt = freeze(output)
    assert receipt["status"] == "ready"
    assert receipt["case_count"] == 3
    assert receipt["ledger_created"] is False
    assert (PROTOCOL, JUDGE_OUTPUT_CAP, EXPECTED_MODEL_CALLS, MAX_REQUESTS, ACCOUNTING_UNITS) == (
        "locomo-confirmation-judge-sensitivity-v1",
        4096,
        3,
        6,
        100_000,
    )
