"""V2 keeps the frozen study shape while changing only its bounded failure fix."""

from experiments.memory_validation import multisession_runner_v2 as target
from experiments.memory_validation.multisession_protocol import ARMS


def _records_with_92_sessions():
    from tests.experiments.test_multisession_protocol import samples

    records = samples()
    for sample, count in zip(records, (31, 32, 29)):
        turn = sample["conversation"].pop("session_1")
        sample["conversation"].pop("session_1_date_time")
        sample["conversation"] = {
            "session_" + str(index): [dict(item, dia_id=f"D{index}:1") for item in turn[:1]]
            for index in range(1, count + 1)
        }
        for question in sample["qa"]:
            question["answer"] = "host-only-gold-sentinel"
    return records


def test_v2_freezes_only_the_extraction_cap_and_budget_changes(tmp_path, monkeypatch):
    dataset = tmp_path / "locomo10.json"
    target.write_json(dataset, _records_with_92_sessions())
    monkeypatch.setattr(target, "SOURCE_SHA256", target.file_sha(dataset))
    environment = tmp_path / "environment.json"
    target.write_json(
        environment,
        {
            "status": "passed",
            "critical_versions": {"mem0ai": "2.2.1"},
            "versions": {"mem0ai": "2.2.1"},
        },
    )

    result = target.prepare(
        dataset,
        tmp_path / "plan",
        tmp_path / "run",
        tmp_path / "gold",
        ledger_path=tmp_path / "ledger.json",
        environment_path=environment,
        model_dir=tmp_path / "model",
        campaign_id="locomo-v2-test",
    )
    plan = target.verify_plan(tmp_path / "plan")

    assert result["expected_model_calls"] == 332
    assert plan["protocol"] == "locomo-multisession-four-arm-v2"
    assert plan["arms"] == list(ARMS)
    assert plan["top_k"] == 8
    assert plan["context_chars"] == 6000
    assert plan["budget"]["max_requests"] == 384
    assert plan["budget"]["accounting_units"] == 5_000_000
    assert plan["budget"]["output_caps"] == {"extract": 8192, "answer": 1024, "judge": 512}
    assert plan["budget"]["v1_terminal_failure"]["total_tokens"] == 11_351
    assert plan["budget"]["v1_terminal_failure"]["status"] == "preserved_not_replayed"
    questions = [
        row
        for entry in plan["conversations"]
        for row in target.read_json(tmp_path / "plan" / entry["file"])["questions"]
    ]
    assert len(questions) == 30
    assert all("evidence_session_count" in row for row in questions)
