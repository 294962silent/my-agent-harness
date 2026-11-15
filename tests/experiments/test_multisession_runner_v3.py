"""V3 changes only bounded observation and inherited budget state."""

import asyncio
import json
import os
import subprocess
import sys

import pytest

from experiments.memory_validation import multisession_runner_v3 as target
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


def _prior_ledger(path, campaign, requests):
    path.write_text(
        json.dumps(
            {
                "limit_micro_usd": 30_000_000,
                "pricing": {"input_per_million": "0.30", "output_per_million": "1.20"},
                "max_requests": 16,
                "time_cap": 600,
                "runs": {
                    row["run_id"]: {
                        "started_at": row["created_at"],
                        "blocked": False,
                        "limits": {"max_requests": 1, "time_cap": 180},
                        "campaign_id": campaign,
                    }
                    for row in requests
                },
                "requests": requests,
                "campaigns": {campaign: {"spend_ceiling_micro_usd": 30_000_000, "blocked": True}},
            }
        ),
        encoding="utf-8",
    )


def _request(request_id, run_id, prompt, completion, total, created):
    return {
        "request_id": request_id,
        "run_id": run_id,
        "model": "deepseek-v4.1-flash",
        "provider": "openai",
        "purpose": "experiment_extract",
        "status": "ok",
        "input_token_bound": prompt + 4096,
        "max_output_tokens": 8192,
        "held_micro_usd": 10_000,
        "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total},
        "cost_micro_usd": 5_000,
        "created_at": created,
        "finished_at": created + 1,
    }


def test_v3_inherits_exact_terminal_usage_and_never_reuses_old_identities(tmp_path, monkeypatch):
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
    v1 = tmp_path / "v1-ledger.json"
    _prior_ledger(
        v1,
        "preserved-v1",
        [_request("v1-request", "extract.same", 4, 2, 6, 10)],
    )
    v2 = tmp_path / "v2-ledger.json"
    _prior_ledger(
        v2,
        "preserved-v2",
        [
            _request("v2-request-1", "extract.same", 4, 2, 6, 20),
            _request("v2-request-2", "extract.next", 5, 3, 8, 30),
        ],
    )
    initial_v3 = tmp_path / "v3-ledger.json"
    _prior_ledger(
        initial_v3,
        "preserved-v3",
        [_request("v3-request", "extract.v3length", 10, 9, 19, 40)],
    )

    result = target.prepare(
        dataset,
        tmp_path / "plan",
        tmp_path / "run",
        tmp_path / "gold",
        ledger_path=tmp_path / "ledger.json",
        environment_path=environment,
        model_dir=tmp_path / "model",
        campaign_id="locomo-v3-test",
        prior_ledgers=(v1, v2, initial_v3),
    )
    plan = target.verify_plan(tmp_path / "plan")
    ledger = target.read_json(tmp_path / "ledger.json")

    assert result["expected_model_calls"] == 332
    assert plan["protocol"] == "locomo-multisession-four-arm-v3"
    assert plan["arms"] == list(ARMS)
    assert plan["top_k"] == 8
    assert plan["context_chars"] == 6000
    assert plan["budget"]["max_requests"] == 384
    assert plan["budget"]["accounting_units"] == 5_000_000
    assert plan["budget"]["output_caps"] == {"extract": 32768, "answer": 1024, "judge": 512}
    assert plan["budget"]["inherited_terminal_usage"] == {
        "requests": 4,
        "prompt_tokens": 23,
        "completion_tokens": 16,
        "total_tokens": 39,
        "conservative_cost_usd": 0.02,
        "call_identities": ["extract.next", "extract.same", "extract.v3length"],
    }
    assert plan["recovery_revision"] == 1
    assert plan["budget"]["call_namespace"] == "v3r1."
    assert target.call_identity("extract", "s_demo") == "extract.v3r1.s_demo"
    assert target.call_identity("answer", "q", "raw_lexical") == "answer.v3r1.q.raw_lexical"
    assert target.call_identity("judge", "q", "facts_mem0") == "judge.v3r1.q.facts_mem0"
    assert plan["observability"]["model_request_retries"] == 0
    assert "bounded exponential polling" in plan["observability"]["first_two_extractions"]
    assert len(ledger["requests"]) == 4
    assert ledger["runs"] == {}
    assert ledger["campaigns"] == {}
    assert {row["request_id"] for row in ledger["requests"]} == {
        "v1-request",
        "v2-request-1",
        "v2-request-2",
        "v3-request",
    }
    assert target.token_state(ledger) == {
        "requests": 4,
        "used_tokens": 39,
        "held_tokens": 0,
        "unknown_or_inflight": False,
    }


def test_v3_freezes_prior_ledger_hashes_and_keeps_question_arm_gold_contract(tmp_path, monkeypatch):
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
    v1 = tmp_path / "v1-ledger.json"
    _prior_ledger(v1, "preserved-v1", [_request("v1-request", "extract.v1", 4, 2, 6, 10)])
    v2 = tmp_path / "v2-ledger.json"
    _prior_ledger(
        v2,
        "preserved-v2",
        [
            _request("v2-request-1", "extract.v2a", 4, 2, 6, 20),
            _request("v2-request-2", "extract.v2b", 5, 3, 8, 30),
        ],
    )
    target.prepare(
        dataset,
        tmp_path / "plan",
        tmp_path / "run",
        tmp_path / "gold",
        ledger_path=tmp_path / "ledger.json",
        environment_path=environment,
        model_dir=tmp_path / "model",
        campaign_id="locomo-v3-test",
        prior_ledgers=(v1, v2),
    )
    plan_root = tmp_path / "plan"
    questions = [
        row
        for entry in target.read_json(plan_root / "plan.json")["conversations"]
        for row in target.read_json(plan_root / entry["file"])["questions"]
    ]
    assert len(questions) == 30
    assert all("evidence_session_count" in row for row in questions)
    assert {row["cross_session"] for row in questions} <= {True, False}

    v1.write_text(v1.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ValueError, match="inherited prior ledger changed"):
        target.verify_plan(plan_root)


def test_v3_budget_reserves_the_full_extraction_ceiling(tmp_path):
    ledger = target.TokenLedgerV3(tmp_path / "ledger.json", campaign_id="campaign")
    ledger.register_run("extract.v3r1.long", max_requests=1, time_cap=600)
    request_id = ledger.reserve(
        "extract.v3r1.long",
        input_bound=1000,
        max_output=32_768,
        model="deepseek-v4.1-flash",
        provider="openai",
        purpose="experiment_extract",
    )
    assert ledger.snapshot()["requests"][0]["max_output_tokens"] == 32_768
    assert ledger.settle(
        request_id,
        usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        status="ok",
    )


def test_v3_lineage_deduplicates_rows_and_rejects_conflicting_request_ids(tmp_path):
    first = tmp_path / "v2.json"
    _prior_ledger(first, "v2", [_request("shared", "extract.v2", 4, 2, 6, 10)])
    derived = tmp_path / "v3.json"
    _prior_ledger(
        derived,
        "v3",
        [
            _request("shared", "extract.v2", 4, 2, 6, 10),
            _request("new", "extract.v3", 5, 3, 8, 20),
        ],
    )
    target.seed_inherited_ledger(tmp_path / "inherited.json", (first, derived))
    inherited = target.read_json(tmp_path / "inherited.json")
    assert [row["request_id"] for row in inherited["requests"]] == ["shared", "new"]

    conflicting = tmp_path / "conflict.json"
    _prior_ledger(conflicting, "conflict", [_request("shared", "extract.other", 5, 3, 8, 30)])
    with pytest.raises(ValueError, match="duplicated with different content"):
        target.seed_inherited_ledger(tmp_path / "bad.json", (first, conflicting))


def _owner_close_payload():
    return {
        "runtime_close_complete": True,
        "export_errors": 0,
        "close_errors": 0,
        "dropped": 0,
        "runtime_alive_after_close": False,
        "llm_clients_closed": True,
    }


def test_owner_close_receipt_is_idempotent_for_same_owner_and_content(tmp_path):
    path = tmp_path / "owner-close.json"
    payload = _owner_close_payload()
    first = target.write_owner_close_receipt(path, owner="campaign-a", payload=payload)
    original = path.read_bytes()
    second = target.write_owner_close_receipt(path, owner="campaign-a", payload=payload)

    assert first == second == payload
    assert path.read_bytes() == original
    receipt = target.read_json(path)
    assert receipt["schema_version"] == target.OWNER_CLOSE_RECEIPT_SCHEMA
    assert receipt["owner"] == "campaign-a"


def test_owner_close_receipt_is_idempotent_after_process_restart(tmp_path):
    path = tmp_path / "owner-close.json"
    payload = _owner_close_payload()
    target.write_owner_close_receipt(path, owner="campaign-a", payload=payload)
    original = path.read_bytes()
    script = (
        "import json, sys\n"
        "from experiments.memory_validation.multisession_runner_v3 import write_owner_close_receipt\n"
        "write_owner_close_receipt(sys.argv[1], owner=sys.argv[2], payload=json.loads(sys.argv[3]))\n"
    )
    environment = dict(os.environ)
    pythonpath = [str(target.REPO_ROOT), str(target.REPO_ROOT / "src")]
    if environment.get("PYTHONPATH"):
        pythonpath.append(environment["PYTHONPATH"])
    environment["PYTHONPATH"] = os.pathsep.join(pythonpath)
    completed = subprocess.run(
        [sys.executable, "-c", script, str(path), "campaign-a", json.dumps(payload)],
        cwd=target.REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert path.read_bytes() == original


def test_owner_close_receipt_rejects_content_and_owner_conflicts(tmp_path):
    path = tmp_path / "owner-close.json"
    payload = _owner_close_payload()
    target.write_owner_close_receipt(path, owner="campaign-a", payload=payload)

    with pytest.raises(target.OwnerCloseConflict, match="conflicting content"):
        target.write_owner_close_receipt(
            path,
            owner="campaign-a",
            payload={**payload, "dropped": 1},
        )
    with pytest.raises(target.OwnerCloseOwnershipError, match="different owner"):
        target.write_owner_close_receipt(path, owner="campaign-b", payload=payload)


def test_owner_close_receipt_accepts_exact_legacy_payload_and_rejects_other_files(tmp_path):
    legacy = tmp_path / "legacy.json"
    payload = _owner_close_payload()
    target.write_json(legacy, payload)
    assert target.write_owner_close_receipt(legacy, owner="campaign-a", payload=payload) == payload

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(target.OwnerCloseConflict, match="unreadable"):
        target.write_owner_close_receipt(malformed, owner="campaign-a", payload=payload)


def test_owner_close_error_is_not_swallowed_and_writes_no_receipt(tmp_path):
    path = tmp_path / "owner-close.json"

    async def failed_close():
        raise OSError("synthetic close failure")

    with pytest.raises(OSError, match="synthetic close failure"):
        asyncio.run(target.finalize_owner_close(path, owner="campaign-a", close=failed_close))
    assert not path.exists()


def test_unsettled_reservation_is_preserved_and_never_replayed(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    output = tmp_path / "output"
    ledger = target.TokenLedgerV3(ledger_path, campaign_id="campaign")
    ledger.register_run("extract.v3r1.crash", max_requests=1, time_cap=600)
    ledger.reserve(
        "extract.v3r1.crash",
        input_bound=100,
        max_output=50,
        model="deepseek-v4.1-flash",
        provider="openai",
        purpose="experiment_extract",
    )

    first = target.record_unsettled_stop(output, ledger)
    original = (output / "STOPPED.json").read_bytes()
    second = target.record_unsettled_stop(output, target.TokenLedgerV3(
        ledger_path,
        campaign_id="campaign",
    ))
    assert first["status"] == second["status"] == "stopped"
    assert first["reason"] == second["reason"] == "pre_dispatch_inflight"
    assert (output / "STOPPED.json").read_bytes() == original

    with pytest.raises(target.BudgetStop, match="unknown usage"):
        second_ledger = target.TokenLedgerV3(ledger_path, campaign_id="campaign")
        second_ledger.reserve(
            "extract.v3r1.replay",
            input_bound=10,
            max_output=10,
            model="deepseek-v4.1-flash",
            provider="openai",
            purpose="experiment_extract",
        )


def test_conflicting_stopped_receipt_fails_closed(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    output = tmp_path / "output"
    ledger = target.TokenLedgerV3(ledger_path, campaign_id="campaign")
    ledger.register_run("extract.v3r1.crash", max_requests=1, time_cap=600)
    ledger.reserve(
        "extract.v3r1.crash",
        input_bound=100,
        max_output=50,
        model="deepseek-v4.1-flash",
        provider="openai",
        purpose="experiment_extract",
    )
    target.record_unsettled_stop(output, ledger)
    target.write_json(
        output / "STOPPED.json",
        {"status": "stopped", "reason": "different_incident"},
        replace=True,
    )

    with pytest.raises(target.OwnerCloseConflict, match="conflicts"):
        target.record_unsettled_stop(output, ledger)


def test_unknown_settlement_is_preserved_and_not_reported_as_zero_usage(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    output = tmp_path / "output"
    ledger = target.TokenLedgerV3(ledger_path, campaign_id="campaign")
    ledger.register_run("extract.v3r1.unknown", max_requests=1, time_cap=600)
    request_id = ledger.reserve(
        "extract.v3r1.unknown",
        input_bound=100,
        max_output=50,
        model="deepseek-v4.1-flash",
        provider="openai",
        purpose="experiment_extract",
    )
    assert not ledger.settle(request_id, usage=None, status="error")

    stopped = target.record_unsettled_stop(output, ledger)
    state = target.token_state(ledger.snapshot())
    assert stopped["reason"] == "pre_dispatch_inflight"
    assert state["unknown_or_inflight"] is True
    assert state["held_tokens"] > 0
