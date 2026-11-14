"""No-network end-to-end four-arm contracts with a real token ledger/owner."""

import asyncio
from contextlib import contextmanager
from pathlib import Path
import json

import pytest

from experiments.benchmark_live.budget import BudgetStop
from experiments.benchmark_live.observation import ExperimentLLMOwner, SessionScopeSink
from experiments.memory_validation.runner import TokenLedger, token_state
from experiments.memory_validation import multisession_runner as target
from my_agent_llm.observation_export import BoundedExporter
from my_agent_llm.observation_runtime import ObservationRuntime
from tests.experiments.test_multisession_adapter import SyncRaw


def owner_for(raw):
    @contextmanager
    def propagate(**kw):
        yield

    scoped = SessionScopeSink(lambda _: None, campaign_id="campaign", propagate=propagate)
    return ExperimentLLMOwner(
        raw, runtime=ObservationRuntime(BoundedExporter(scoped)), scope_sink=scoped, required=True
    )


class ScriptRaw(SyncRaw):
    def __init__(self, contents):
        super().__init__("")
        self.contents, self.seen = iter(contents), []

    def chat(self, messages, **kwargs):
        self.seen.append([m.content for m in messages])
        self.content = next(self.contents)
        return super().chat(messages, **kwargs)


class FakeIndex:
    def __init__(self):
        self.memory, self.llm, self.rows, self.closed = self, object(), [], False
        self.enable_graph = False

    def add(self, messages, *, user_id, metadata, infer):
        if infer:
            result = json.loads(self.llm.generate_response(messages, response_format={"type": "json_object"}))
            texts = [m["text"] for m in result["memory"]]
        else:
            texts = [m["content"] for m in messages]
        added = []
        for text in texts:
            identity = str(len(self.rows))
            self.rows.append({"id": identity, "memory": text, "metadata": metadata})
            added.append({"id": identity, "event": "ADD", "memory": text})
        return {"results": added}

    def get_all(self, *, filters, top_k):
        return {"results": self.rows[:top_k]}

    def search(self, query, scope):
        return self.rows[:8]

    def close(self):
        self.closed = True


def test_ranked_session_metrics_deduplicate_while_preserving_rank():
    recall, reciprocal_rank = target._ranked_session_metrics(
        ["session_2", "session_2", "session_4"], ["session_4", "session_2"]
    )
    assert recall == 1
    assert reciprocal_rank == 1


def fixture_input():
    docs = [
        {
            "key": "s1",
            "session_id": "session_1",
            "part": 0,
            "text": "private history A",
            "turn_ids": ["D1:1"],
            "sha256": "a",
        },
        {
            "key": "s2",
            "session_id": "session_2",
            "part": 0,
            "text": "private history B",
            "turn_ids": ["D2:1"],
            "sha256": "b",
        },
    ]
    questions = [
        {
            "question_id": "q1",
            "question": "private question",
            "category": 1,
            "evidence_ids": ["D1:1"],
            "question_type": "multi_hop",
        }
    ]
    return {"sessions": docs, "raw_chunks": docs, "questions": questions}


def test_one_native_ingest_snapshot_then_four_matched_arms_and_blind_judges(tmp_path):
    raw = ScriptRaw(
        ['{"memory":[{"text":"private fact A"}]}', '{"memory":[{"text":"private fact B"}]}']
        + [v for _ in range(4) for v in ("private prediction", '{"correct":true}')]
    )
    owner = owner_for(raw)
    ledger = TokenLedger(tmp_path / "ledger.json", campaign_id="campaign", max_requests=384)
    indexes, gates = [], []

    def factory():
        item = FakeIndex()
        indexes.append(item)
        return item

    def gate(root, response, *, task_id):
        gates.append((root, task_id))

    slots = target.PaidSlots(owner, ledger, output=tmp_path / "run", verify=lambda: None, wall_deadline=10**12)
    entry = {"conversation_id": "conv-47", "split": "dev"}
    try:
        rows = target.run_conversation(
            entry,
            fixture_input(),
            {"q1": "host gold"},
            slots=slots,
            factory=factory,
            gate=gate,
            output=tmp_path / "run",
        )
        assert raw.calls == 10 and token_state(ledger.snapshot())["used_tokens"] == 60
        assert len(gates) == 2 and all(x.closed for x in indexes)
        assert [r["arm"] for r in rows] == list(target.ARMS)
        assert all(r["correct"] for r in rows)
        assert rows[0]["context_sha256"] == rows[1]["context_sha256"]
        assert set(rows[2]["chunk_keys"]) == set(rows[3]["chunk_keys"])
        assert rows[0]["evidence_recall"] == 1 and rows[2]["evidence_recall"] is None
        # Extraction sees history only, answer sees question/context only, judge sees host gold.
        assert all("host gold" not in str(m) for m in raw.seen[:2])
        for index in range(2, 10, 2):
            assert "host gold" not in str(raw.seen[index])
            assert "host gold" in str(raw.seen[index + 1])
            assert not any(arm in str(raw.seen[index + 1]) for arm in target.ARMS)
        public = (tmp_path / "run" / "safe" / "conv-47.json").read_text()
        assert "private history" not in public and "host gold" not in public
        assert "private prediction" not in public and "private question" not in public
        completed_hash = target.file_sha(tmp_path / "run" / "safe" / "conv-47.json")
        with pytest.raises(BudgetStop):
            target.run_conversation(
                entry,
                fixture_input(),
                {"q1": "host gold"},
                slots=slots,
                factory=factory,
                gate=gate,
                output=tmp_path / "run",
            )
        assert raw.calls == 10 and target.file_sha(tmp_path / "run" / "safe" / "conv-47.json") == completed_hash
    finally:
        asyncio.run(owner.aclose())


def test_length_answer_and_invalid_judge_retain_all_four_denominators(tmp_path):
    raw = ScriptRaw(["answer", "not json"] * 4)
    raw.finish_reason = "length"
    owner = owner_for(raw)
    ledger = TokenLedger(tmp_path / "ledger.json", campaign_id="campaign", max_requests=384)
    slots = target.PaidSlots(owner, ledger, output=tmp_path / "run", verify=lambda: None, wall_deadline=10**12)
    context = {
        "context": "text",
        "context_chars": 4,
        "chunk_keys": ["s1"],
        "turn_ids": ["D1:1"],
        "session_ids": ["session_1"],
    }
    try:
        rows = target.evaluate_question(
            fixture_input()["questions"][0], "host gold", {arm: context for arm in target.ARMS}, slots=slots
        )
        assert len(rows) == 4 and raw.calls == 8
        assert all(not r["correct"] and r["answer_status"] == "length" and r["judge_status"] == "length" for r in rows)
        assert token_state(ledger.snapshot())["requests"] == 8
    finally:
        asyncio.run(owner.aclose())


def test_started_slot_and_unknown_usage_stop_without_repeating_provider(tmp_path):
    raw = ScriptRaw(["answer"])
    owner = owner_for(raw)
    ledger = TokenLedger(tmp_path / "ledger.json", campaign_id="campaign", max_requests=384)
    slots = target.PaidSlots(owner, ledger, output=tmp_path / "run", verify=lambda: None, wall_deadline=10**12)
    try:
        slots.call(
            "answer.q1.raw_lexical",
            [{"role": "user", "content": "question"}],
            arm="raw_lexical",
            stage="answer",
            task_id="q1",
            max_tokens=1024,
        )
        with pytest.raises(BudgetStop):
            slots.call("answer.q1.raw_lexical", [], arm="raw_lexical", stage="answer", task_id="q1", max_tokens=1024)
        assert raw.calls == 1
        # A crashed reservation is not cleared or converted to zero-cost success.
        ledger.register_run("old", max_requests=1, time_cap=180)
        ledger.reserve("old", input_bound=8, max_output=8, purpose="old", model="offline", provider="offline")
        with pytest.raises(BudgetStop):
            slots.call("answer.q2.raw_lexical", [], arm="raw_lexical", stage="answer", task_id="q2", max_tokens=1024)
        assert raw.calls == 1 and token_state(ledger.snapshot())["unknown_or_inflight"]
    finally:
        asyncio.run(owner.aclose())


def test_frozen_inventory_detects_added_mutable_files_and_source_mutations(tmp_path):
    plan = tmp_path / "plan"
    plan.mkdir()
    source = tmp_path / "module.py"
    source.write_text("old")
    target.write_json(plan / "inputs.json", {"text": "private input"})
    target.seal_plan(plan, {"sources": [{"path": str(source), "sha256": target.file_sha(source)}]})
    target.verify_plan(plan)
    (plan / "ledger.tokens.lock").touch()
    with pytest.raises(ValueError):
        target.verify_plan(plan)
    (plan / "ledger.tokens.lock").unlink()
    source.write_text("new")
    with pytest.raises(ValueError):
        target.verify_plan(plan)


@pytest.mark.parametrize("suffix", ["", ".lock", ".tokens.lock", ".study.lock"])
def test_ledger_and_real_lock_paths_cannot_collide_with_immutable_plan(tmp_path, suffix):
    ledger = tmp_path / "budget.json"
    with pytest.raises(ValueError):
        target.validate_locations(Path(str(ledger) + suffix) / "child", tmp_path / "run", ledger)


def test_prepare_freezes_thirty_original_qa_and_keeps_gold_out_of_plan(tmp_path, monkeypatch):
    from tests.experiments.test_multisession_protocol import samples

    records = samples()
    for sample, count in zip(records, (31, 32, 29)):
        turn = sample["conversation"].pop("session_1")
        sample["conversation"].pop("session_1_date_time")
        sample["conversation"] = {
            "session_" + str(i): [dict(t, dia_id=f"D{i}:1") for t in turn[:1]] for i in range(1, count + 1)
        }
        for question in sample["qa"]:
            question["answer"] = "host-only-gold-sentinel"
    dataset = tmp_path / "locomo10.json"
    target.write_json(dataset, records)
    monkeypatch.setattr(target, "SOURCE_SHA256", target.file_sha(dataset))
    environment = tmp_path / "environment.json"
    target.write_json(
        environment,
        {
            "status": "passed",
            "critical_versions": {"mem0ai": "2.2.1"},
            "versions": {"mem0ai": "2.2.1"},
            "executable": "/private/python",
        },
    )
    result = target.prepare(
        dataset,
        tmp_path / "plan",
        tmp_path / "run",
        tmp_path / "gold",
        ledger_path=tmp_path / "budget" / "ledger.json",
        environment_path=environment,
        model_dir=tmp_path / "model",
        campaign_id="campaign",
    )
    plan = target.verify_plan(tmp_path / "plan")
    assert result["expected_model_calls"] == 332
    assert plan["budget"]["max_requests"] == 384 and plan["budget"]["accounting_units"] == 3_000_000
    assert plan["budget"]["output_caps"] == {"extract": 2048, "answer": 1024, "judge": 512}
    assert plan["counts"] == {
        "dev_qa": 6,
        "test_qa": 24,
        "sessions": 92,
        "extract_calls": 92,
        "answer_calls": 120,
        "judge_calls": 120,
        "expected_model_calls": 332,
    }
    assert len(target.read_json(tmp_path / "gold" / "references.json")) == 30
    for file in (tmp_path / "plan").rglob("*.json"):
        assert "host-only-gold-sentinel" not in file.read_text(encoding="utf-8")
    assert not (tmp_path / "budget" / "ledger.json").exists()
    references = tmp_path / "gold" / "references.json"
    references.write_text("{}")
    with pytest.raises(ValueError):
        target.verify_plan(tmp_path / "plan")


def test_no_new_dispatch_when_source_changes_between_native_extractions(tmp_path):
    raw = ScriptRaw(['{"memory":[]}'])
    owner = owner_for(raw)
    ledger = TokenLedger(tmp_path / "ledger.json", campaign_id="campaign", max_requests=384)
    source = tmp_path / "source"
    source.write_text("old")

    def verify():
        if source.read_text() != "old":
            raise ValueError("source changed")

    slots = target.PaidSlots(owner, ledger, output=tmp_path / "run", verify=verify, wall_deadline=10**12)
    try:
        from experiments.memory_validation.multisession_adapter import GuardedMem0LLM

        adapter = GuardedMem0LLM(slots, ledger, run_id="extract.s1", task_id="s1", audit_path=tmp_path / "audit")
        source.write_text("changed")
        with pytest.raises(ValueError):
            adapter.generate_response([{"role": "user", "content": "source"}], response_format={"type": "json_object"})
        assert raw.calls == 0 and not ledger.snapshot()["requests"]
    finally:
        asyncio.run(owner.aclose())
