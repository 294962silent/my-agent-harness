import json
import sys

import pytest

from experiments.memory_validation.runner import local_path, select_new, TokenLedger, token_state, verify_retrieval
from experiments.longmemeval.harness_corpus import QUESTION_TYPES, render_chunks
from experiments.benchmark_live.budget import BudgetStop


def test_selection_disjoint_balanced_and_order_independent():
    rows = [{"question_id": f"{kind}-{i}", "question_type": kind} for kind in QUESTION_TYPES for i in range(8)]
    rows += [{"question_id": f"{kind}-new_abs", "question_type": kind} for kind in QUESTION_TYPES]
    excluded = {f"{kind}-{i}" for kind in QUESTION_TYPES for i in range(5)}
    chosen = select_new(rows, excluded)
    assert chosen == select_new(list(reversed(rows)), excluded)
    assert len(chosen) == 12
    assert not {r["question_id"] for r in chosen} & excluded
    assert all(sum(r["question_type"] == kind for r in chosen) == 2 for kind in QUESTION_TYPES)
    assert sum(r["question_id"].endswith("_abs") for r in chosen) == 6


def test_selection_rejects_duplicates_and_sparse_types():
    with pytest.raises(ValueError):
        select_new([], set())
    with pytest.raises(ValueError):
        select_new([{"question_id": "x", "question_type": QUESTION_TYPES[0]}] * 2, set())


def test_token_ledger_restores_global_request_and_token_guards(tmp_path):
    ledger = TokenLedger(tmp_path / "ledger.json", campaign_id="offline-test", max_tokens=12, max_requests=2)
    ledger.register_run("first", max_requests=1, time_cap=180)
    request = ledger.reserve("first", input_bound=4, max_output=4, model="offline", provider="offline", purpose="test")
    assert ledger.settle(request, usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}, status="ok")
    restored = TokenLedger(tmp_path / "ledger.json", campaign_id="offline-test", max_tokens=12, max_requests=2)
    restored.register_run("next", max_requests=1, time_cap=180)
    with pytest.raises(BudgetStop):
        restored.reserve("next", input_bound=4, max_output=4, model="offline", provider="offline", purpose="test")
    assert token_state(restored.snapshot())["used_tokens"] == 5


def test_unknown_total_or_inflight_blocks_whole_study(tmp_path):
    ledger = TokenLedger(tmp_path / "ledger.json", campaign_id="offline-test")
    ledger.register_run("a", max_requests=1, time_cap=180)
    ledger.reserve("a", input_bound=10, max_output=10, model="offline", provider="offline", purpose="test")
    ledger.register_run("b", max_requests=1, time_cap=180)
    with pytest.raises(BudgetStop):
        ledger.reserve("b", input_bound=1, max_output=1, model="offline", provider="offline", purpose="test")


def test_local_path_roundtrip_cross_platform(monkeypatch):
    windows = "C:/Users/MLL/Desktop/my-agent-harness/.worktrees/interview-redesign"
    wsl = "/mnt/c/Users/MLL/Desktop/my-agent-harness/.worktrees/interview-redesign"
    monkeypatch.setattr(sys, "platform", "linux")
    assert local_path(windows).as_posix() == wsl
    monkeypatch.setattr(sys, "platform", "win32")
    assert local_path(wsl).as_posix() == windows


def test_retrieval_context_cannot_be_forged():
    corpus = [
        {
            "key": "lme_" + "a" * 24,
            "session_id": "sid_test",
            "date": "2024-01-01",
            "valid_from": "2024-01-01",
            "text": "evidence",
        }
    ]
    # none needs no corpus serialization or reference material.
    row = {"question_id": "q", "arm": "none", "valid": True, **render_chunks(corpus, [], 6000)}
    verify_retrieval({"question_id": "q"}, [row, {**row, "arm": "lexical"}, {**row, "arm": "mem0"}], corpus, 6000)
    row["context"] = "fabricated evidence"
    with pytest.raises(ValueError):
        verify_retrieval({"question_id": "q"}, [row, {**row, "arm": "lexical"}, {**row, "arm": "mem0"}], corpus, 6000)


def test_exact_72_requests_and_3m_total_are_global(tmp_path):
    ledger = TokenLedger(tmp_path / "requests.json", campaign_id="offline")
    for i in range(72):
        ledger.register_run(str(i), max_requests=1, time_cap=180)
        rid = ledger.reserve(str(i), input_bound=1, max_output=1, model="offline", provider="offline", purpose="test")
        assert ledger.settle(rid, usage={"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}, status="ok")
    ledger.register_run("overflow", max_requests=1, time_cap=180)
    with pytest.raises(BudgetStop):
        ledger.reserve("overflow", input_bound=1, max_output=1, model="offline", provider="offline", purpose="test")
    ledger = TokenLedger(tmp_path / "tokens.json", campaign_id="offline")
    ledger.register_run("exact", max_requests=1, time_cap=180)
    rid = ledger.reserve(
        "exact", input_bound=2999999, max_output=1, model="offline", provider="offline", purpose="test"
    )
    assert ledger.settle(
        rid, usage={"prompt_tokens": 2999999, "completion_tokens": 1, "total_tokens": 3000000}, status="ok"
    )
    ledger.register_run("overflow", max_requests=1, time_cap=180)
    with pytest.raises(BudgetStop):
        ledger.reserve("overflow", input_bound=1, max_output=1, model="offline", provider="offline", purpose="test")


@pytest.mark.parametrize("total", [None, True, -1, 1])
def test_missing_or_inconsistent_total_blocks(tmp_path, total):
    ledger = TokenLedger(tmp_path / "ledger.json", campaign_id="offline")
    ledger.register_run("a", max_requests=1, time_cap=180)
    rid = ledger.reserve("a", input_bound=10, max_output=10, model="offline", provider="offline", purpose="test")
    assert not ledger.settle(
        rid, usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": total}, status="ok"
    )
    assert ledger.campaign_status()["blocked"]


@pytest.fixture
def frozen_fixture(tmp_path, monkeypatch):
    """Synthetic offline contract fixture; never claimed as benchmark evidence."""
    from experiments.memory_validation import runner as study
    from my_agent_core import memory_local

    source = tmp_path / "longmemeval_s_cleaned.json"
    old = tmp_path / "old.json"
    model = tmp_path / "model"
    model.mkdir()
    (model / "manifest.json").write_text("{}")
    records = []
    excluded = []
    for kind in QUESTION_TYPES:
        for i in range(7):
            qid = f"{kind}-{i}"
            records.append(
                {
                    "question_id": qid,
                    "question_type": kind,
                    "question": "Which value?",
                    "question_date": "2024-01-01",
                    "answer": "GOLD_SENTINEL",
                    "answer_session_ids": ["RAW_SID"],
                    "has_answer": True,
                    "haystack_dates": ["2024-01-01"],
                    "haystack_session_ids": ["RAW_SID"],
                    "haystack_sessions": [[{"role": "user", "content": "A history value."}]],
                }
            )
            if i < 5:
                excluded.append({"question_id": qid, "question_type": kind})
    source.write_text(json.dumps(records))
    old.write_text(json.dumps(excluded))
    monkeypatch.setattr(study, "SOURCE_SHA256", study.file_hash(source))
    monkeypatch.setattr(memory_local, "verify_model_files", lambda path: {"test_fixture": True})
    plan = tmp_path / "plan"
    args = [
        "prepare",
        "--source",
        str(source),
        "--output",
        str(plan),
        "--old-rows",
        str(old),
        "--model-dir",
        str(model),
        "--source-revision",
        "offline-fixture",
        "--source-license",
        "offline-fixture",
        "--ledger-path",
        str(tmp_path / "ledger.json"),
        "--campaign-id",
        "offline-contract",
    ]
    assert study.main(args) == 0
    assert study.main(["verify", "--plan", str(plan)]) == 0
    return study, plan, tmp_path


def test_cli_prepare_verify_excludes_gold_from_worker_inputs(frozen_fixture):
    study, root, _ = frozen_fixture
    p = study.verify_plan(root)
    for e in p["questions"]:
        for field in ("question_file", "corpus_file"):
            text = (root / e[field]).read_text()
            assert all(
                marker not in text for marker in ("GOLD_SENTINEL", "RAW_SID", "answer_session_ids", "has_answer")
            )
    with pytest.raises(ValueError):
        study.prepare(
            "unused",
            root,
            old_rows="unused",
            model_dir="unused",
            source_revision="x",
            source_license="x",
            ledger_path=root / "l",
            campaign_id="x",
        )


@pytest.mark.parametrize(
    "overlap",
    [
        "inside", "same", "ledger_ancestor", "lock_same", "study_lock_same", "lock_ancestor",
        "study_lock_ancestor", "tokens_lock_same", "tokens_lock_ancestor",
    ],
)
def test_prepare_rejects_mutable_ledger_or_lock_overlap(frozen_fixture, overlap):
    study, _, tmp = frozen_fixture
    output = tmp / "new-plan"
    ledger = tmp / "mutable" / "ledger.json"
    if overlap == "inside":
        ledger = output / "nested" / "ledger.json"
    elif overlap == "same":
        ledger = output
    elif overlap == "ledger_ancestor":
        output = ledger / "plan"
    elif overlap == "lock_same":
        output = ledger.with_name(ledger.name + ".lock")
    elif overlap == "study_lock_same":
        output = ledger.with_name(ledger.name + ".study.lock")
    elif overlap == "lock_ancestor":
        output = ledger.with_name(ledger.name + ".lock") / "plan"
    elif overlap == "study_lock_ancestor":
        output = ledger.with_name(ledger.name + ".study.lock") / "plan"
    elif overlap == "tokens_lock_same":
        output = ledger.with_name(ledger.name + ".tokens.lock")
    else:
        output = ledger.with_name(ledger.name + ".tokens.lock") / "plan"
    with pytest.raises(ValueError, match="mutable ledger"):
        study.prepare(
            tmp / "longmemeval_s_cleaned.json",
            output,
            old_rows=tmp / "old.json",
            model_dir=tmp / "model",
            source_revision="offline-fixture",
            source_license="offline-fixture",
            ledger_path=ledger,
            campaign_id="offline-layout-guard",
        )
    assert not output.exists(), "Invalid layout must fail before plan files are created"


def test_prepare_allows_sibling_ledger_and_locks_without_inventory_drift(frozen_fixture, monkeypatch):
    from filelock import FileLock
    from experiments.benchmark_live import budget

    study, _, tmp = frozen_fixture
    output = tmp / "new-plan"
    ledger = tmp / "new-plan-run" / "ledger.json"
    temporary_paths = []
    mkstemp = budget.tempfile.mkstemp

    def record_temporary(*args, **kwargs):
        fd, name = mkstemp(*args, **kwargs)
        temporary_paths.append(study.Path(name))
        return fd, name

    monkeypatch.setattr(budget.tempfile, "mkstemp", record_temporary)
    study.prepare(
        tmp / "longmemeval_s_cleaned.json",
        output,
        old_rows=tmp / "old.json",
        model_dir=tmp / "model",
        source_revision="offline-fixture",
        source_license="offline-fixture",
        ledger_path=ledger,
        campaign_id="offline-layout-guard",
    )
    plan_hash = study.file_hash(output / "plan.json")
    live = TokenLedger(ledger, campaign_id="offline-layout-guard")
    live.register_run("offline", max_requests=1, time_cap=180)
    request = live.reserve("offline", input_bound=2, max_output=2, model="offline", provider="offline", purpose="test")
    assert live.settle(request, usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}, status="ok")
    outer_lock = FileLock(str(ledger) + ".study.lock", timeout=0)
    assert {live.lock.lock_file, live.study_lock.lock_file, outer_lock.lock_file} == {
        str(ledger) + suffix for suffix in (".lock", ".tokens.lock", ".study.lock")
    }
    with outer_lock:
        assert study.verify_plan(output)["ledger_path"] == str(ledger.resolve())
    assert study.file_hash(output / "plan.json") == plan_hash
    assert temporary_paths
    assert all(path.parent == ledger.parent and path.name.startswith(".budget-") for path in temporary_paths)
    assert all(not path.is_relative_to(output) for path in temporary_paths)



def test_paid_cli_is_blind_idempotent_and_summarizes_invalid_judge(frozen_fixture, monkeypatch, capsys):
    from my_agent_llm import Config, Response

    study, plan, tmp = frozen_fixture
    p = study.verify_plan(plan)
    retrieval = study.phase_root(plan, tmp / "retrieval", "retrieval", {})
    for e in p["questions"]:
        corpus = study.read(plan / e["corpus_file"])
        rows = []
        for arm in study.ARMS:
            rows.append(
                {
                    "question_id": e["question_id"],
                    "arm": arm,
                    "valid": True,
                    "total_seconds": 0.0,
                    **render_chunks(corpus, [] if arm == "none" else [corpus[0]["key"]], 6000),
                }
            )
        study.write(retrieval / "results" / f"{study.tag(e['question_id'])}.json", {"rows": rows})
    study.inventory(retrieval)
    seen = []

    class OfflineLLM:
        model = p["model"]
        config = Config(
            model=p["model"], provider="openai", base_url=p["base_url"], max_retries=0, max_tokens=1024, timeout=180
        )

        async def achat(self, messages, **kwargs):
            seen.append(messages)
            if len(seen) <= 36:
                content = "offline hypothesis"
                reason = "stop"
            elif len(seen) == 37:
                content = ""
                reason = "length"
            else:
                content = "yes"
                reason = "stop"
            return Response(
                model=self.model,
                content=content,
                usage={"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
                finish_reason=reason,
            )

    monkeypatch.setattr(study, "create_live_llm", lambda **kwargs: OfflineLLM())
    answerargs = ["answer", "--plan", str(plan), "--retrieval", str(retrieval), "--output", str(tmp / "answers")]
    assert study.main(answerargs + ["--max-questions", "1"]) == 0
    assert len(seen) == 3
    assert study.main(answerargs + ["--max-questions", "1", "--resume"]) == 0
    assert len(seen) == 6
    assert study.main(answerargs + ["--resume"]) == 0
    assert len(seen) == 36
    assert study.main(answerargs + ["--resume"]) == 0
    assert len(seen) == 36
    judgeargs = [
        "judge",
        "--plan",
        str(plan),
        "--retrieval",
        str(retrieval),
        "--answers",
        str(tmp / "answers"),
        "--output",
        str(tmp / "judges"),
    ]
    assert study.main(judgeargs) == 0
    assert len(seen) == 72
    for messages in seen[36:]:
        body = " ".join(m.content for m in messages)
        assert "GOLD_SENTINEL" in body
        assert all(marker not in body for marker in ("lexical", "mem0", "sid_", "A history value."))
    assert study.main(judgeargs + ["--resume"]) == 0
    assert len(seen) == 72
    summary = study.summarize(plan, retrieval, tmp / "answers", tmp / "judges")
    assert summary["ledger"]["requests"] == 72 and summary["ledger"]["used_tokens"] == 216
    assert sum(g["available"] for g in summary["arms"].values()) == 35
    assert sum(g["pending_or_unavailable"] for g in summary["arms"].values()) == 1
    assert (
        study.main(
            [
                "summarize",
                "--plan",
                str(plan),
                "--retrieval",
                str(retrieval),
                "--answers",
                str(tmp / "answers"),
                "--judges",
                str(tmp / "judges"),
            ]
        )
        == 0
    )


def test_verify_plan_rejects_tampered_limit(frozen_fixture):
    study, plan, _ = frozen_fixture
    original = study.read(plan / "plan.json")
    tampered = {**original, "limits": {**original["limits"], "requests": 73}}
    study.write(plan / "plan.json", tampered, replace=True)
    with pytest.raises(ValueError):
        study.verify_plan(plan)
