"""Fixed-history retrieval ablation contracts; fixtures are not benchmark scores."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


TYPES = (
    "single-session-user",
    "single-session-assistant",
    "single-session-preference",
    "multi-session",
    "temporal-reasoning",
    "knowledge-update",
)


def record(qid="q0", qtype="single-session-user"):
    return {
        "question_id": qid,
        "question_type": qtype,
        "question": "Where is the telescope?",
        "question_date": "2024/02/01",
        "haystack_dates": ["2024/01/01", "2024/01/02"],
        "haystack_session_ids": ["s1", "s2"],
        "haystack_sessions": [
            [{"role": "user", "content": "My telescope is in the attic."}],
            [{"role": "assistant", "content": "Another unrelated observation."}],
        ],
        "answer": "GOLD_SENTINEL",
        "has_answer": True,
        "answer_session_ids": ["GOLD_SESSION_SENTINEL"],
    }


def records():
    return [record(f"{kind}-{i}" + ("_abs" if i == 0 else ""), kind) for kind in TYPES for i in range(8)]


def test_selection_is_frozen_balanced_and_prioritizes_one_abstention():
    from experiments.longmemeval.harness_corpus import select_records

    chosen = select_records(records(), seed=23)
    assert len(chosen) == 30 and len({r["question_id"] for r in chosen}) == 30
    assert [r["question_id"] for r in chosen] == [
        r["question_id"] for r in select_records(list(reversed(records())), seed=23)
    ]
    for kind in TYPES:
        group = [r for r in chosen if r["question_type"] == kind]
        assert len(group) == 5 and sum(r["question_id"].endswith("_abs") for r in group) == 1


def test_corpus_uses_every_session_but_never_gold_and_is_deterministic():
    from experiments.longmemeval.harness_corpus import build_corpus, answer_prompt, session_alias

    source = record()
    corpus = build_corpus(source, chunk_chars=100)
    assert {c["session_id"] for c in corpus} == {session_alias("s1"), session_alias("s2")}
    assert "GOLD" not in json.dumps(corpus)
    assert corpus == build_corpus(source, chunk_chars=100)
    messages = answer_prompt({k: source[k] for k in ("question", "question_date")}, "")
    assert "GOLD" not in json.dumps(messages) and "2024/02/01" in json.dumps(messages)


def test_plan_is_new_only_hash_frozen_and_separates_references(tmp_path):
    from experiments.longmemeval.harness_ablation import prepare_plan, verify_plan

    source = tmp_path / "longmemeval_s_cleaned.json"
    raw = json.dumps(records()).encode()
    source.write_bytes(raw)
    output = tmp_path / "plan"
    prepare_plan(source, output, source_sha256=hashlib.sha256(raw).hexdigest(), source_revision="fixture-revision")
    plan = verify_plan(output)
    assert len(plan["questions"]) == 30 and plan["language"] == "original_english"
    for path in (output / "corpora").iterdir():
        assert "GOLD" not in path.read_text()
    assert "GOLD" in (output / "references.json").read_text()
    with pytest.raises(FileExistsError):
        prepare_plan(source, output, source_sha256=hashlib.sha256(raw).hexdigest(), source_revision="fixture-revision")
    corpus = next((output / "corpora").iterdir())
    corpus.write_text("[]")
    with pytest.raises(ValueError):
        verify_plan(output)


def test_real_production_lexical_and_mem0_errors_cannot_masquerade_as_success(tmp_path):
    from experiments.longmemeval.harness_corpus import build_corpus
    from experiments.longmemeval.harness_retrieval import retrieve_question

    class Failing:
        status = {"circuit_open": True}

        async def search(self, **kwargs):
            raise TimeoutError("fixture")

        async def aclose(self, timeout=0):
            return True

    rows = asyncio.run(
        retrieve_question(record(), build_corpus(record()), tmp_path, index_factory=lambda *a, **kw: Failing())
    )
    lexical = next(r for r in rows if r["arm"] == "lexical")
    semantic = next(r for r in rows if r["arm"] == "mem0")
    assert lexical["valid"] and lexical["chunk_keys"]
    assert not semantic["valid"] and semantic["context"] == "" and semantic["error_type"] == "TimeoutError"


def test_valid_semantic_empty_suppresses_production_lexical_fallback(tmp_path):
    from experiments.longmemeval.harness_corpus import build_corpus
    from experiments.longmemeval.harness_retrieval import retrieve_question

    class Empty:
        status = {"circuit_open": False}

        async def search(self, **kwargs):
            return []

        async def aclose(self, timeout=0):
            return True

    rows = asyncio.run(
        retrieve_question(record(), build_corpus(record()), tmp_path, index_factory=lambda *a, **kw: Empty())
    )
    semantic = next(r for r in rows if r["arm"] == "mem0")
    assert semantic["valid"] and semantic["empty"] and semantic["context"] == "" and semantic["chunk_keys"] == []


def test_session_metrics_and_abstention_are_separate():
    from experiments.longmemeval.harness_corpus import retrieval_metrics

    value = retrieval_metrics(["s2", "s1", "s1"], {"answer_session_ids": ["s1"], "is_abstention": False}, valid=True)
    assert value["recall_at_8"] == 1 and value["mrr"] == 0.5
    abstain = retrieval_metrics([], {"answer_session_ids": [], "is_abstention": True}, valid=True)
    assert abstain["recall_at_8"] is None and abstain["reason"] == "abstention_no_evidence"
    assert (
        retrieval_metrics([], {"answer_session_ids": ["s1"], "is_abstention": False}, valid=False)["reason"]
        == "invalid_retrieval"
    )


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux seccomp contract")
def test_os_network_lockdown_allows_asyncio_but_denies_internet_socket():
    root = Path(__file__).resolve().parents[2]
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(root / "src") + os.pathsep + str(root)}
    script = (
        "import asyncio,socket;from experiments.longmemeval.harness_retrieval import deny_network;"
        "deny_network();asyncio.run(asyncio.to_thread(lambda:42));"
        "\ntry: socket.socket(socket.AF_INET,socket.SOCK_STREAM)"
        "\nexcept PermissionError: print('blocked')"
        "\nelse: raise AssertionError('Internet socket allowed')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0 and result.stdout.strip() == "blocked"


def paid_fixture(tmp_path):
    from experiments.benchmark_live.budget import BudgetLedger
    from experiments.longmemeval.harness_ablation import prepare_plan, read, tag, phase_root, write, inventory

    source = tmp_path / "longmemeval_s_cleaned.json"
    raw = json.dumps(records()).encode()
    source.write_bytes(raw)
    ledger = tmp_path / "ledger.json"
    BudgetLedger(ledger)
    plan_root = tmp_path / "plan"
    prepare_plan(
        source,
        plan_root,
        source_sha256=hashlib.sha256(raw).hexdigest(),
        source_revision="fixture",
        ledger_path=ledger,
        campaign_ceiling_usd=1,
    )
    entry = read(plan_root / "plan.json")["questions"][0]
    from experiments.longmemeval.harness_corpus import render_chunks

    corpus = read(plan_root / entry["corpus_file"])
    retrieval = phase_root(plan_root, tmp_path / "retrieval", "retrieval", {"fixture": True})
    write(
        retrieval / "results" / (tag(entry["question_id"]) + ".json"),
        {
            "network_boundary": "linux_seccomp_no_non_unix_socket_or_connect",
            "rows": [
                {
                    "question_id": entry["question_id"],
                    "arm": arm,
                    "valid": arm != "mem0",
                    **render_chunks(corpus, [corpus[0]["key"]] if arm == "lexical" else [], 6000),
                    "total_seconds": 0.1,
                }
                for arm in ("none", "lexical", "mem0")
            ],
        },
    )
    inventory(retrieval)
    return plan_root, retrieval, entry, ledger


def test_paid_stages_use_real_guard_one_request_ids_and_never_retry_invalid_judge(tmp_path):
    from experiments.longmemeval.harness_paid import paid_phase
    from experiments.longmemeval.harness_ablation import read
    from my_agent_llm import Config, LLM
    from my_agent_llm.models import Response

    plan_root, retrieval, entry, ledger_path = paid_fixture(tmp_path)
    calls = []

    class Provider:
        verdict = False

        async def achat(self, messages, **kwargs):
            calls.append([m.content for m in messages])
            assert kwargs["max_tokens"] == 1024 and kwargs["temperature"] == 0
            return Response(
                content="maybe" if self.verdict else "fixture answer",
                model="deepseek-v4.1-flash",
                usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            )

    raw = LLM(Config(provider="openai", api_key="offline", model="deepseek-v4.1-flash", max_retries=0))
    provider = Provider()
    raw._provider = provider
    answers, judges = tmp_path / "answers", tmp_path / "judges"
    asyncio.run(paid_phase("answer", plan_root, retrieval, answers, max_questions=1, raw_llm=raw))
    assert len(calls) == 2 and "GOLD" not in json.dumps(calls)
    provider.verdict = True
    asyncio.run(paid_phase("judge", plan_root, retrieval, judges, answers=answers, max_questions=1, raw_llm=raw))
    assert len(calls) == 4 and "GOLD_SENTINEL" in json.dumps(calls[-1])
    states = [read(p) for p in (judges / "trials").glob("*.json")]
    assert all(s["status"] == "unavailable" for s in states)
    asyncio.run(
        paid_phase("judge", plan_root, retrieval, judges, answers=answers, max_questions=1, raw_llm=raw, resume=True)
    )
    assert len(calls) == 4
    ledger = read(ledger_path)
    assert len(ledger["requests"]) == 4
    assert all(r["limits"]["max_requests"] == 1 for r in ledger["runs"].values())


def test_retrieval_resume_and_malformed_worker_output_are_not_success(tmp_path):
    from experiments.longmemeval.harness_ablation import prepare_plan, retrieve_phase, read, write

    source = tmp_path / "longmemeval_s_cleaned.json"
    raw = json.dumps(records()).encode()
    source.write_bytes(raw)
    plan_root = tmp_path / "plan"
    prepare_plan(source, plan_root, source_sha256=hashlib.sha256(raw).hexdigest())
    calls = []

    def broken(command, result_path):
        calls.append(command)
        result_path.write_text("{partial")
        return 0

    output = tmp_path / "retrieval"
    first = retrieve_phase(plan_root, output, model_dir=tmp_path, max_questions=1, _worker=broken)
    assert all(first["arms"][a]["invalid"] == 1 for a in ("none", "lexical", "mem0"))
    assert len(list((output / "rejected").glob("*.json"))) == 1
    second = retrieve_phase(plan_root, output, model_dir=tmp_path, max_questions=1, _worker=broken, resume=True)
    assert len(calls) == 2 and all(second["arms"][a]["invalid"] == 2 for a in ("none", "lexical", "mem0"))
    phase = read(output / "phase.json")
    write(output / "phase.json", {**phase, "threads": 999}, replace=True)
    with pytest.raises(ValueError):
        retrieve_phase(plan_root, output, model_dir=tmp_path, max_questions=1, _worker=broken, resume=True)


def test_budget_stop_halts_before_next_arm_and_resume_does_not_retry(tmp_path, monkeypatch):
    from experiments.longmemeval.harness_paid import paid_phase
    from experiments.benchmark_live.budget import BudgetStop, GuardedLLM
    from experiments.longmemeval.harness_ablation import read
    from my_agent_llm import Config, LLM

    plan_root, retrieval, entry, ledger = paid_fixture(tmp_path)
    calls = []

    async def stopped(self, *args, **kwargs):
        calls.append(self)
        raise BudgetStop("fixture stop")

    monkeypatch.setattr(GuardedLLM, "achat", stopped)
    raw = LLM(Config(provider="openai", api_key="offline", model="deepseek-v4.1-flash", max_retries=0))
    root = tmp_path / "answers"
    result = asyncio.run(paid_phase("answer", plan_root, retrieval, root, max_questions=1, raw_llm=raw))
    assert result["status"] == "stopped" and len(calls) == 1
    states = list((root / "trials").glob("*.json"))
    assert len(states) == 1 and read(states[0])["reason"] == "BudgetStop"
    resumed = asyncio.run(paid_phase("answer", plan_root, retrieval, root, max_questions=1, raw_llm=raw, resume=True))
    assert resumed["status"] == "stopped" and len(calls) == 1


@pytest.mark.parametrize(
    "changed_file", ["experiments/longmemeval/harness_paid.py", "experiments/benchmark_live/budget.py"]
)
def test_frozen_plan_rejects_changed_paid_execution_source_before_call(tmp_path, monkeypatch, changed_file):
    import shutil
    from experiments.longmemeval import harness_ablation as ablation
    from experiments.longmemeval.harness_paid import paid_phase

    plan_root, retrieval, entry, ledger = paid_fixture(tmp_path)
    repo = Path(ablation.__file__).resolve().parents[2]
    clone = tmp_path / "source-copy"
    for proof in ablation.implementation():
        target = clone / proof["file"]
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(repo / proof["file"], target)
    # Point hash discovery at identical copies, then mutate only the temporary copy.
    monkeypatch.setattr(ablation, "__file__", str(clone / "experiments/longmemeval/harness_ablation.py"))
    ablation.verify_plan(plan_root)
    target = clone / changed_file
    target.write_bytes(target.read_bytes() + b"\n# simulated changed execution logic\n")
    before = ledger.read_bytes()
    output = tmp_path / "must-not-start"
    with pytest.raises(ValueError, match="frozen source/protocol implementation differs"):
        asyncio.run(paid_phase("answer", plan_root, retrieval, output, max_questions=1))
    assert not output.exists() and ledger.read_bytes() == before


def test_anonymous_session_identity_survives_plan_real_retrieval_and_reader_prompt(tmp_path, monkeypatch):
    from experiments.longmemeval import harness_retrieval as retrieval_module
    from experiments.longmemeval.harness_ablation import prepare_plan, retrieve_phase, read, write
    from experiments.longmemeval.harness_corpus import answer_prompt, session_alias

    data = records()
    for row in data:
        row["question_id"] = row["question_id"].replace("_abs", "-ordinary")
        row["haystack_session_ids"] = ["answer_goldmarker", "random_original_session"]
        row["answer_session_ids"] = ["answer_goldmarker"]
        row["answer"] = "GOLD_PRIVATE_CANARY"
    source = tmp_path / "longmemeval_s_cleaned.json"
    raw = json.dumps(data).encode()
    source.write_bytes(raw)
    plan_root = tmp_path / "plan"
    plan = prepare_plan(source, plan_root, source_sha256=hashlib.sha256(raw).hexdigest())
    forbidden = (b"answer_goldmarker", b"random_original_session", b"GOLD_PRIVATE_CANARY")
    references = read(plan_root / "references.json")
    assert references[plan["questions"][0]["question_id"]]["session_aliases"]["answer_goldmarker"] == session_alias(
        "answer_goldmarker"
    )
    for directory in ("questions", "corpora"):
        for path in (plan_root / directory).glob("*.json"):
            assert all(marker not in path.read_bytes() for marker in forbidden)
    original_store = retrieval_module._store
    stores_checked, index_inputs = [], []

    def checked_store(*args, **kwargs):
        store = original_store(*args, **kwargs)
        files = [p for p in Path(args[0]).rglob("*") if p.is_file()]
        assert files
        for path in files:
            assert all(marker not in path.read_bytes() for marker in forbidden)
        stores_checked.append(True)
        return store

    monkeypatch.setattr(retrieval_module, "_store", checked_store)

    class Empty:
        status = {"circuit_open": False}

        async def search(self, **kwargs):
            index_inputs.append(json.dumps(kwargs).encode())
            assert all(marker not in index_inputs[-1] for marker in forbidden)
            return []

        async def aclose(self, timeout=0):
            return True

    def worker(command, output):
        question = read(command[command.index("--question") + 1])
        corpus = read(command[command.index("--corpus") + 1])
        assert all(marker not in json.dumps([question, corpus]).encode() for marker in forbidden)
        rows = asyncio.run(
            retrieval_module.retrieve_question(
                question, corpus, tmp_path / "work", index_factory=lambda **kwargs: Empty()
            )
        )
        for row in rows:
            assert all(
                marker not in json.dumps(answer_prompt(question, row["context"])).encode() for marker in forbidden
            )
        write(output, {"network_boundary": "linux_seccomp_no_non_unix_socket_or_connect", "rows": rows})
        return 0

    result = retrieve_phase(plan_root, tmp_path / "retrieval", model_dir=tmp_path, max_questions=1, _worker=worker)
    assert stores_checked and index_inputs
    lexical = next(r for r in result["rows"] if r["arm"] == "lexical")
    assert lexical["valid"] and lexical["metrics"]["recall_at_8"] == 1
    assert lexical["session_ids"][0].startswith("sid_")


def test_raw_session_identity_cannot_bypass_anonymized_fact_boundary():
    from experiments.longmemeval.harness_corpus import build_corpus, chunk_value

    corpus = build_corpus(record())
    corpus[0]["session_id"] = "answer_goldmarker"
    with pytest.raises(ValueError, match="must be anonymized"):
        chunk_value(corpus[0])


def test_paid_resume_accepts_plan_append_but_rejects_changed_consumed_context(tmp_path):
    from experiments.longmemeval.harness_ablation import read, write, inventory, tag
    from experiments.longmemeval.harness_paid import paid_phase
    from experiments.longmemeval.harness_corpus import render_chunks
    from my_agent_llm import Config, LLM
    from my_agent_llm.models import Response

    plan_root, retrieval, first, ledger = paid_fixture(tmp_path)
    calls = []

    class Provider:
        async def achat(self, messages, **kwargs):
            calls.append(messages)
            return Response(
                content="fixture answer",
                model="deepseek-v4.1-flash",
                usage={"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            )

    raw = LLM(Config(provider="openai", api_key="offline", model="deepseek-v4.1-flash", max_retries=0))
    raw._provider = Provider()
    output = tmp_path / "answers"
    asyncio.run(paid_phase("answer", plan_root, retrieval, output, max_questions=1, raw_llm=raw))
    assert len(calls) == 2
    second = read(plan_root / "plan.json")["questions"][1]
    original_path = retrieval / "results" / (tag(first["question_id"]) + ".json")
    value = read(original_path)
    corpus = read(plan_root / second["corpus_file"])
    for row in value["rows"]:
        row["question_id"] = second["question_id"]
        row.update(render_chunks(corpus, [corpus[0]["key"]] if row["arm"] == "lexical" else [], 6000))
    write(retrieval / "results" / (tag(second["question_id"]) + ".json"), value)
    inventory(retrieval)
    asyncio.run(paid_phase("answer", plan_root, retrieval, output, max_questions=1, raw_llm=raw, resume=True))
    assert len(calls) == 4 and len(read(ledger)["requests"]) == 4
    value = read(original_path)
    corpus = read(plan_root / first["corpus_file"])
    value["rows"][1].update(render_chunks(corpus, [corpus[1]["key"]], 6000))
    write(original_path, value, replace=True)
    inventory(retrieval)
    before = ledger.read_bytes()
    with pytest.raises(ValueError, match="previously consumed retrieval changed"):
        asyncio.run(paid_phase("answer", plan_root, retrieval, output, max_questions=1, raw_llm=raw, resume=True))
    assert len(calls) == 4 and ledger.read_bytes() == before
