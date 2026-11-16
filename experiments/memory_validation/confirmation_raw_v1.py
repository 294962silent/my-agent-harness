"""Fresh LoCoMo confirmation study for three frozen raw-retrieval arms."""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import statistics
import time

from experiments.memory_validation.budget_v3 import (
    LIVE_BASE_URL,
    LIVE_MODEL,
    BudgetStop,
    create_live_llm,
)
from experiments.memory_validation.confirmation_protocol_v1 import (
    ARMS,
    CATEGORIES,
    CATEGORY_NAMES,
    CONTEXT_CHARS,
    EXCLUDED_CONVERSATIONS,
    PROTOCOL,
    RAW_CHUNK_CHARS,
    RRF_K,
    SEED,
    TOP_K,
    freeze_selection,
    reciprocal_rank_fusion,
    session_metrics,
)
from experiments.memory_validation.followup_common import (
    FirstTwoReadback,
    FollowupPaidStage,
    LANGFUSE_CLOUD_BASE_URL,
    file_sha,
    install_live_credential,
    paired_comparisons,
    read_json,
    request_usage,
    require_langfuse_credentials,
    required_owner,
    seal,
    usage_sum,
    verify_seal,
    write_json,
)
from experiments.memory_validation.multisession_protocol import (
    bm25_order,
    raw_chunks,
    render_context,
    sha,
)
from experiments.memory_validation.multisession_runner_v3 import (
    ANSWER_SYSTEM,
    JUDGE_SYSTEM,
    _environment_value,
    _evidence_sessions,
    _judge,
    _model_manifest,
    TokenLedgerV3,
)
from experiments.memory_validation.runner import token_state


SOURCE_SHA256 = "79fa87e90f04081343b8c8debecb80a9a6842b76a7aa537dc9fdf651ea698ff4"
SOURCE_REVISION = "3eb6f2c585f5e1699204e3c3bdf7adc5c28cb376"
SOURCE_LICENSE = "CC BY-NC 4.0"
MODEL_REPO = "Qwen/Qwen3-Embedding-0.6B"
MODEL_REVISION = "97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3"
LANGFUSE_PROJECT_ID = "cmwg5sagt00ksad0j6au0463n"
CALL_NAMESPACE = "confirmation.v1."
ACCOUNTING_UNITS = 2_000_000
MAX_REQUESTS = 180
EXPECTED_MODEL_CALLS = 144
REQUEST_TIMEOUT_SECONDS = 180
WALL_CLOCK_SECONDS = 2 * 3600
OUTPUT_CAPS = {"answer": 1024, "judge": 1024}
COMPARISONS = (
    ("raw_lexical", "raw_semantic_qwen"),
    ("raw_lexical", "raw_hybrid_rrf"),
    ("raw_semantic_qwen", "raw_hybrid_rrf"),
)
SOURCE_FILES = (
    "experiments/memory_validation/followup_common.py",
    "experiments/memory_validation/confirmation_protocol_v1.py",
    "experiments/memory_validation/confirmation_raw_v1.py",
    "experiments/memory_validation/budget_v3.py",
    "experiments/memory_validation/multisession_embedding.py",
    "experiments/memory_validation/multisession_index.py",
    "experiments/memory_validation/multisession_observation.py",
    "experiments/memory_validation/multisession_observation_v3.py",
    "experiments/memory_validation/multisession_protocol.py",
    "experiments/memory_validation/multisession_runner_v3.py",
    "experiments/memory_validation/qwen_seccomp_worker.py",
    "src/my_agent_core/_memory_qwen_worker.py",
    "src/my_agent_core/memory_local.py",
    "src/my_agent_core/memory_qwen.py",
    "src/my_agent_llm/client.py",
    "src/my_agent_llm/langfuse_export.py",
    "src/my_agent_llm/observation.py",
    "src/my_agent_llm/observation_export.py",
    "src/my_agent_llm/observation_runtime.py",
)
REPO_ROOT = Path(__file__).resolve().parents[2]


def now_utc():
    return datetime.now(timezone.utc).isoformat()


def _safe_id(value):
    allowed = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_.:-"
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or any(ch not in allowed for ch in value):
        raise ValueError("opaque identifier is invalid")
    return value


def _related(a, b):
    return a == b or a in b.parents or b in a.parents


def _validate_locations(plan_root, gold_root, run_root, ledger):
    plan_root, gold_root, run_root, ledger = (
        Path(path).resolve() for path in (plan_root, gold_root, run_root, ledger)
    )
    for left, right in ((plan_root, gold_root), (plan_root, run_root), (gold_root, run_root)):
        if _related(left, right):
            raise ValueError("immutable plan/gold and mutable run roots must be disjoint")
    for root in (plan_root, gold_root, run_root):
        if _related(root, ledger):
            raise ValueError("ledger must be disjoint from plan/gold/run roots")
    return plan_root, gold_root, run_root, ledger


def _implementation_sources():
    rows = []
    for relative in SOURCE_FILES:
        path = REPO_ROOT / relative
        if not path.is_file():
            raise ValueError("frozen implementation source is missing")
        rows.append({"path": str(path), "sha256": file_sha(path)})
    return rows


def prepare(
    dataset,
    plan_root,
    gold_root,
    run_root,
    ledger,
    environment_path,
    model_dir,
    *,
    campaign_id,
    linux_python="/home/mll/.cache/my-agent-bench/qwen3-probe-venv/bin/python",
    cuda_visible_devices="2",
):
    dataset, plan_root, gold_root, run_root, ledger, environment_path, model_dir = (
        Path(path).resolve()
        for path in (dataset, plan_root, gold_root, run_root, ledger, environment_path, model_dir)
    )
    _validate_locations(plan_root, gold_root, run_root, ledger)
    if any(path.exists() for path in (plan_root, gold_root, run_root, ledger)):
        raise ValueError("new plan/gold/run/ledger roots are required")
    if dataset.name != "locomo10.json" or file_sha(dataset) != SOURCE_SHA256:
        raise ValueError("exact pinned LoCoMo source required")
    if not str(cuda_visible_devices).isdigit():
        raise ValueError("CUDA_VISIBLE_DEVICES must be one explicit device index")
    environment = read_json(environment_path)
    versions = _environment_value(environment)
    from my_agent_core.memory_qwen import verify_model_files

    verify_model_files(model_dir)
    manifest = _model_manifest(model_dir)
    if manifest.get("sha256") is None:
        raise ValueError("Qwen model harness manifest is required")
    records = read_json(dataset)
    selection = freeze_selection(records)
    if len(selection) != 2 or {item["conversation_id"] for item in selection} & set(EXCLUDED_CONVERSATIONS):
        raise ValueError("confirmation selection leaves the pre-result rule")
    by_id = {record["sample_id"]: record for record in records}
    plan_root.mkdir(parents=True)
    gold_root.mkdir(parents=True)
    conversations, data_rows, references = [], [], {}
    category_counts = Counter()
    total_sessions = 0
    for chosen in selection:
        conversation_id = chosen["conversation_id"]
        sample = by_id[conversation_id]
        sessions = raw_chunks(sample, limit=RAW_CHUNK_CHARS)
        if not sessions:
            raise ValueError("selected conversation has no raw chunks")
        questions = []
        for index in chosen["indices"]:
            original = sample["qa"][index]
            answer, evidence, category = original.get("answer"), original.get("evidence"), original.get("category")
            if (
                category not in CATEGORIES
                or not isinstance(original.get("question"), str)
                or not original["question"].strip()
                or not isinstance(answer, str)
                or not answer.strip()
                or not isinstance(evidence, list)
                or not evidence
                or any(not isinstance(turn, str) for turn in evidence)
            ):
                raise ValueError("selected official QA lacks a direct frozen field")
            qid = "q_" + sha([PROTOCOL, conversation_id, index])[:24]
            evidence_session_ids = _evidence_sessions(sessions, evidence)
            question = {
                "question_id": qid,
                "qa_index": index,
                "question": original["question"],
                "question_sha256": sha(original["question"]),
                "category": category,
                "question_type": CATEGORY_NAMES[category],
                "evidence_ids": list(evidence),
                "evidence_sha256": sha(evidence),
                "evidence_session_ids": evidence_session_ids,
                "evidence_session_count": len(evidence_session_ids),
                "cross_session": len(evidence_session_ids) > 1,
            }
            questions.append(question)
            category_counts[category] += 1
            references[qid] = {
                "answer": answer,
                "evidence_ids": list(evidence),
                "evidence_session_ids": evidence_session_ids,
                "category": category,
                "conversation_id": conversation_id,
            }
        questions.sort(key=lambda row: row["qa_index"])
        conversation = {
            "conversation_id": conversation_id,
            "raw_chunks": sessions,
            "questions": questions,
        }
        relative = "conversations/" + conversation_id + ".json"
        write_json(plan_root / relative, conversation)
        data_rows.append({"path": relative, "sha256": file_sha(plan_root / relative), "bytes": (plan_root / relative).stat().st_size})
        unique_sessions = len({chunk["session_id"] for chunk in sessions})
        total_sessions += unique_sessions
        conversations.append(
            {
                "conversation_id": conversation_id,
                "file": relative,
                "questions": len(questions),
                "raw_chunks": len(sessions),
                "sessions": unique_sessions,
            }
        )
    if (
        len(references) != 24
        or category_counts != Counter({1: 8, 2: 8, 4: 8})
        or sum(item["questions"] for item in conversations) != 24
    ):
        raise ValueError("confirmation denominator or category balance differs")
    references_path = gold_root / "references.json"
    write_json(references_path, references)
    write_json(
        gold_root / "manifest.json",
        {"references.json": {"bytes": references_path.stat().st_size, "sha256": file_sha(references_path)}},
    )
    environment_copy = json.loads(json.dumps(environment, sort_keys=True))
    write_json(plan_root / "environment.json", environment_copy)
    plan = {
        "schema_version": 1,
        "protocol": PROTOCOL,
        "created_at": now_utc(),
        "campaign_id": _safe_id(campaign_id),
        "ledger_path": str(ledger),
        "selection": {
            "seed": SEED,
            "excluded_conversations": list(EXCLUDED_CONVERSATIONS),
            "rule": "metadata-only eligible conversation shuffle; four evidence-labeled QA per category",
            "source_fields": ["sample_id", "qa.category", "qa.evidence"],
            "model_scores_used": False,
            "conversations": [item["conversation_id"] for item in selection],
        },
        "source": {
            "path": str(dataset),
            "sha256": file_sha(dataset),
            "bytes": dataset.stat().st_size,
            "repository": "snap-research/locomo",
            "revision": SOURCE_REVISION,
            "license": SOURCE_LICENSE,
            "file": "data/locomo10.json",
        },
        "environment": {"path": str(environment_path), "critical_versions": versions},
        "gold": {
            "path": str(references_path),
            "sha256": file_sha(references_path),
            "manifest_sha256": file_sha(gold_root / "manifest.json"),
            "bytes": references_path.stat().st_size,
        },
        "sources": _implementation_sources(),
        "data": data_rows,
        "conversations": conversations,
        "counts": {
            "conversations": 2,
            "questions": 24,
            "categories": {str(key): value for key, value in sorted(category_counts.items())},
            "answer_calls": 72,
            "judge_calls": 72,
            "expected_model_calls": EXPECTED_MODEL_CALLS,
            "retrieval_arm_questions": 72,
            "sessions": total_sessions,
            "raw_chunks": sum(item["raw_chunks"] for item in conversations),
        },
        "arms": list(ARMS),
        "retrieval": {
            "raw_chunk_chars": RAW_CHUNK_CHARS,
            "top_k": TOP_K,
            "context_chars": CONTEXT_CHARS,
            "lexical": "BM25 k1=1.5,b=0.75 as inherited; top 8 includes zero-overlap ties",
            "semantic": "local Qwen dense search over the same raw chunks; top 8",
            "hybrid": "RRF over lexical top-8 and semantic top-8; k=60; select top 8; key tie-break",
            "rrf_k": RRF_K,
        },
        "prompts": {"answer_sha256": sha(ANSWER_SYSTEM), "judge_sha256": sha(JUDGE_SYSTEM)},
        "model": LIVE_MODEL,
        "provider": "openai",
        "base_url": LIVE_BASE_URL,
        "temperature": 0,
        "max_retries": 0,
        "request_timeout_seconds": REQUEST_TIMEOUT_SECONDS,
        "output_caps": dict(OUTPUT_CAPS),
        "embedding": {
            "repo": MODEL_REPO,
            "revision": MODEL_REVISION,
            "model_dir": str(model_dir),
            "manifest": manifest,
            "dimensions": 1024,
            "max_length": 4096,
            "batch_size": 1,
            "startup_timeout": 240,
            "request_timeout": 180,
            "python_executable": linux_python,
            "wsl_distribution": "Ubuntu-20.04",
            "cuda_visible_devices": cuda_visible_devices,
            "network": "WSL seccomp no non-unix socket/connect",
        },
        "budget": {
            "accounting_units": ACCOUNTING_UNITS,
            "max_requests": MAX_REQUESTS,
            "expected_model_calls": EXPECTED_MODEL_CALLS,
            "request_headroom": MAX_REQUESTS - EXPECTED_MODEL_CALLS,
            "wall_clock_seconds": WALL_CLOCK_SECONDS,
            "retries": 0,
            "policy": "reserve before each request, settle exactly once, unknown usage stops all later dispatches",
        },
        "observability": {
            "owner": "ExperimentLLMOwnerV3",
            "required": True,
            "cloud_base_url": LANGFUSE_CLOUD_BASE_URL,
            "project_id": LANGFUSE_PROJECT_ID,
            "campaign_session": campaign_id,
            "first_two_calls": "180-second bounded exponential metadata/usage polling; exactly one observation per trace",
            "model_request_retries": 0,
        },
    }
    seal(plan_root, "plan.json", plan)
    return {
        "status": "prepared",
        "plan_sha256": file_sha(plan_root / "plan.json"),
        "conversations": plan["selection"]["conversations"],
        "questions": 24,
        "categories": plan["counts"]["categories"],
    }


def verify_plan(plan_root):
    plan_root = Path(plan_root).resolve()
    plan = verify_seal(plan_root, "plan.json")
    if (
        plan.get("protocol") != PROTOCOL
        or plan.get("arms") != list(ARMS)
        or plan.get("retrieval", {}).get("top_k") != TOP_K
        or plan.get("retrieval", {}).get("context_chars") != CONTEXT_CHARS
        or plan.get("retrieval", {}).get("rrf_k") != RRF_K
        or plan.get("counts", {}).get("questions") != 24
        or plan.get("counts", {}).get("expected_model_calls") != EXPECTED_MODEL_CALLS
        or plan.get("output_caps") != OUTPUT_CAPS
    ):
        raise ValueError("confirmation protocol differs")
    if file_sha(plan["source"]["path"]) != SOURCE_SHA256 or file_sha(plan["source"]["path"]) != plan["source"]["sha256"]:
        raise ValueError("LoCoMo source changed")
    if file_sha(plan["gold"]["path"]) != plan["gold"]["sha256"]:
        raise ValueError("confirmation gold changed")
    for source in plan["sources"]:
        if file_sha(source["path"]) != source["sha256"]:
            raise ValueError("confirmation implementation source changed")
    if file_sha(plan["embedding"]["manifest"]["path"]) != plan["embedding"]["manifest"]["sha256"]:
        raise ValueError("Qwen model manifest changed")
    _environment_value(read_json(plan_root / "environment.json"))
    for row in plan["data"]:
        if file_sha(plan_root / row["path"]) != row["sha256"]:
            raise ValueError("frozen conversation data changed")
    return plan


def _context_path(run_root, question_id):
    return Path(run_root) / "preflight" / "contexts" / (question_id + ".json")


def preflight(plan_root, run_root):
    plan_root, run_root = Path(plan_root).resolve(), Path(run_root).resolve()
    plan = verify_plan(plan_root)
    if run_root.exists():
        raise ValueError("new retrieval preflight run root required")
    run_root.mkdir(parents=True)
    from experiments.memory_validation.multisession_index import create_study_memory

    config = {
        "profile": "local_qwen_gpu_wsl_stdio",
        "model_dir": plan["embedding"]["model_dir"],
        "storage_dir": str(run_root / "index-storage"),
        "python_executable": plan["embedding"]["python_executable"],
        "device": "cuda:0",
        "batch_size": 1,
        "max_length": 4096,
        "startup_timeout": plan["embedding"]["startup_timeout"],
        "request_timeout": plan["embedding"]["request_timeout"],
        "wsl_distribution": plan["embedding"]["wsl_distribution"],
        "cuda_visible_devices": plan["embedding"]["cuda_visible_devices"],
    }
    records, context_files, embedding_status = [], {}, {}
    for entry in plan["conversations"]:
        conversation = read_json(plan_root / entry["file"])
        if conversation.get("conversation_id") != entry["conversation_id"]:
            raise ValueError("preflight conversation identity differs")
        corpus = conversation["raw_chunks"]
        index = create_study_memory(config)
        scope = entry["conversation_id"]
        try:
            embedding_status[scope] = dict(index.memory.embedding_model.status)
            by_memory_id = {}
            for chunk in corpus:
                result = index.memory.add(
                    [{"role": "user", "content": chunk["text"]}],
                    user_id=scope,
                    metadata={"chunk_key": chunk["key"], "source_session_id": chunk["session_id"]},
                    infer=False,
                )
                if len(result.get("results", [])) != 1 or result["results"][0].get("event") != "ADD":
                    raise ValueError("semantic preflight import differs")
                by_memory_id[result["results"][0]["id"]] = chunk["key"]
            for question in conversation["questions"]:
                lexical = bm25_order(corpus, question["question"], top_k=TOP_K)
                started = time.monotonic()
                hits = index.search(question["question"], scope)
                semantic = []
                for hit in hits:
                    if hit.get("id") not in by_memory_id:
                        raise ValueError("semantic search returned a non-frozen chunk")
                    semantic.append(by_memory_id[hit["id"]])
                if len(semantic) != TOP_K or len(set(semantic)) != TOP_K:
                    raise ValueError("semantic candidate budget differs")
                hybrid = reciprocal_rank_fusion(lexical, semantic)
                contexts = {}
                for arm, keys in (
                    ("raw_lexical", lexical),
                    ("raw_semantic_qwen", semantic),
                    ("raw_hybrid_rrf", hybrid),
                ):
                    if len(keys) != TOP_K or len(set(keys)) != TOP_K:
                        raise ValueError("retrieval candidate budget differs")
                    rendered = render_context(corpus, keys, limit=CONTEXT_CHARS)
                    if rendered["context_chars"] > CONTEXT_CHARS or any(key not in {row["key"] for row in corpus} for key in keys):
                        raise ValueError("retrieval candidate boundary differs")
                    contexts[arm] = rendered
                path = _context_path(run_root, question["question_id"])
                write_json(
                    path,
                    {
                        "question_id": question["question_id"],
                        "question_sha256": question["question_sha256"],
                        "candidates": {
                            "raw_lexical": lexical,
                            "raw_semantic_qwen": semantic,
                            "raw_hybrid_rrf": hybrid,
                        },
                        "contexts": contexts,
                    },
                )
                context_files[path.relative_to(run_root).as_posix()] = file_sha(path)
                records.append(
                    {
                        "conversation_id": scope,
                        "question_id": question["question_id"],
                        "semantic_seconds": time.monotonic() - started,
                        "candidates": {
                            arm: {
                                "count": len(contexts[arm]["chunk_keys"]),
                                "keys_sha256": sha(contexts[arm]["chunk_keys"]),
                                "context_sha256": sha(contexts[arm]["context"]),
                                "context_chars": contexts[arm]["context_chars"],
                            }
                            for arm in ARMS
                        },
                    }
                )
        finally:
            index.close()
    if len(records) != 24 or len(context_files) != 24:
        raise ValueError("retrieval preflight denominator differs")
    receipt = {
        "schema_version": 1,
        "status": "passed",
        "protocol": PROTOCOL,
        "created_at": now_utc(),
        "plan_sha256": file_sha(plan_root / "plan.json"),
        "questions": 24,
        "arm_questions": 72,
        "candidate_count_per_arm": TOP_K,
        "max_context_chars": max(
            candidate["context_chars"]
            for record in records
            for candidate in record["candidates"].values()
        ),
        "embedding_status": embedding_status,
        "embedding_invocations": sum(
            item["raw_chunks"] + item["questions"] for item in plan["conversations"]
        ),
        "context_files": context_files,
        "records": records,
        "paid_model_calls": 0,
    }
    write_json(run_root / "PREFLIGHT.json", receipt)
    return {"status": "passed", "questions": 24, "arm_questions": 72, "preflight_sha256": file_sha(run_root / "PREFLIGHT.json")}


def _verify_preflight(plan_root, run_root):
    plan = verify_plan(plan_root)
    receipt = read_json(Path(run_root) / "PREFLIGHT.json")
    if (
        receipt.get("status") != "passed"
        or receipt.get("protocol") != PROTOCOL
        or receipt.get("questions") != 24
        or receipt.get("arm_questions") != 72
        or receipt.get("candidate_count_per_arm") != TOP_K
        or receipt.get("plan_sha256") != file_sha(Path(plan_root) / "plan.json")
    ):
        raise ValueError("retrieval preflight receipt differs")
    for relative, expected in receipt.get("context_files", {}).items():
        if file_sha(Path(run_root) / relative) != expected:
            raise ValueError("preflight context changed")
    return plan, receipt


def freeze(plan_root, run_root):
    plan_root, run_root = Path(plan_root).resolve(), Path(run_root).resolve()
    plan, preflight_receipt = _verify_preflight(plan_root, run_root)
    receipt = {
        "schema_version": 1,
        "status": "ready",
        "protocol": PROTOCOL,
        "campaign_id": plan["campaign_id"],
        "frozen_at": now_utc(),
        "plan_sha256": file_sha(plan_root / "plan.json"),
        "plan_manifest_sha256": file_sha(plan_root / "manifest.json"),
        "preflight_sha256": file_sha(run_root / "PREFLIGHT.json"),
        "preflight_questions": 24,
        "expected_model_calls": EXPECTED_MODEL_CALLS,
        "paid_calls": 0,
        "ledger_created": Path(plan["ledger_path"]).exists() if "ledger_path" in plan else False,
        "embedding_calls": preflight_receipt.get("embedding_invocations"),
    }
    write_json(run_root / "READY.json", receipt)
    return receipt


def _verify_ready(plan_root, run_root):
    plan, preflight_receipt = _verify_preflight(plan_root, run_root)
    ready = read_json(Path(run_root) / "READY.json")
    if (
        ready.get("status") != "ready"
        or ready.get("protocol") != PROTOCOL
        or ready.get("campaign_id") != plan["campaign_id"]
        or ready.get("plan_sha256") != file_sha(Path(plan_root) / "plan.json")
        or ready.get("plan_manifest_sha256") != file_sha(Path(plan_root) / "manifest.json")
        or ready.get("preflight_sha256") != file_sha(Path(run_root) / "PREFLIGHT.json")
    ):
        raise ValueError("READY receipt differs")
    return plan, preflight_receipt, ready


def _question_rows(plan_root, plan):
    result = {}
    for entry in plan["conversations"]:
        conversation = read_json(Path(plan_root) / entry["file"])
        for question in conversation["questions"]:
            if question["question_id"] in result:
                raise ValueError("question identity duplicated")
            result[question["question_id"]] = {**question, "conversation_id": entry["conversation_id"]}
    return result


def _completed(run_root):
    path = Path(run_root) / "status.json"
    if not path.exists():
        return None
    value = read_json(path)
    if value.get("status") != "completed" or value.get("rows") != 72:
        raise ValueError("existing confirmation status differs")
    return value


def run(plan_root, run_root, *, use_configured_credential=False):
    plan_root, run_root = Path(plan_root).resolve(), Path(run_root).resolve()
    existing = _completed(run_root)
    if existing is not None:
        return existing
    plan, preflight_receipt, _ = _verify_ready(plan_root, run_root)
    started_states = list((run_root / "states").glob("*.json")) if (run_root / "states").exists() else []
    if (run_root / "STOPPED.json").exists() or started_states:
        raise BudgetStop("started confirmation study cannot be replayed")
    require_langfuse_credentials()
    if use_configured_credential:
        credential_source = install_live_credential()
    else:
        credential_source = "live_environment"
        if not __import__("os").environ.get("LIVE_BENCH_API_KEY"):
            raise ValueError("LIVE_BENCH_API_KEY must be supplied explicitly")
    ledger = TokenLedgerV3(
        plan["ledger_path"],
        campaign_id=plan["campaign_id"],
        max_tokens=plan["budget"]["accounting_units"],
        max_requests=plan["budget"]["max_requests"],
    )
    raw = owner = owner_close = None
    references = read_json(plan["gold"]["path"])
    try:
        raw = create_live_llm(max_output_tokens=max(OUTPUT_CAPS.values()), timeout=REQUEST_TIMEOUT_SECONDS)
        if raw.model != LIVE_MODEL or raw.config.provider != "openai" or raw.config.max_retries != 0:
            raise ValueError("live client differs from frozen model")
        owner = required_owner(raw, plan["campaign_id"])
        stage = FollowupPaidStage(
            owner,
            ledger,
            output=run_root,
            verify=lambda: _verify_ready(plan_root, run_root),
            wall_deadline=time.time() + plan["budget"]["wall_clock_seconds"],
            request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
        gate = FirstTwoReadback(
            owner,
            campaign=plan["campaign_id"],
            project=LANGFUSE_PROJECT_ID,
            model=LIVE_MODEL,
            receipt=run_root / "first-two-readback.json",
            protocol=PROTOCOL,
        )
        for entry in plan["conversations"]:
            conversation = read_json(plan_root / entry["file"])
            for question in conversation["questions"]:
                qid = question["question_id"]
                context_path = _context_path(run_root, qid)
                if file_sha(context_path) != preflight_receipt["context_files"][context_path.relative_to(run_root).as_posix()]:
                    raise ValueError("retrieval context hash changed before paid run")
                stored = read_json(context_path)
                if stored.get("question_sha256") != question["question_sha256"]:
                    raise ValueError("retrieval context question binding differs")
                rows = []
                for arm in ARMS:
                    context = stored["contexts"][arm]
                    if (
                        context["context_chars"] > CONTEXT_CHARS
                        or len(stored.get("candidates", {}).get(arm, [])) != TOP_K
                        or not set(context["chunk_keys"]).issubset(stored["candidates"][arm])
                    ):
                        raise ValueError("retrieval context differs")
                    answer_id = CALL_NAMESPACE + "answer." + qid + "." + arm
                    judge_id = CALL_NAMESPACE + "judge." + qid + "." + arm
                    answer = stage.call(
                        answer_id,
                        [
                            {"role": "system", "content": ANSWER_SYSTEM},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {"question": question["question"], "conversation_context": context["context"]},
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        arm=arm,
                        stage="answer",
                        task_id=qid,
                        max_tokens=OUTPUT_CAPS["answer"],
                    )
                    gate(answer_id, answer, task_id=qid, arm=arm, stage="answer")
                    answer_status = (
                        "valid"
                        if answer.finish_reason == "stop" and isinstance(answer.content, str) and answer.content.strip()
                        else "length"
                        if answer.finish_reason == "length"
                        else "invalid"
                    )
                    judge = stage.call(
                        judge_id,
                        [
                            {"role": "system", "content": JUDGE_SYSTEM},
                            {
                                "role": "user",
                                "content": json.dumps(
                                    {
                                        "question": question["question"],
                                        "reference_answer": references[qid]["answer"],
                                        "proposed_answer": answer.content,
                                    },
                                    ensure_ascii=False,
                                ),
                            },
                        ],
                        arm=arm,
                        stage="judge",
                        task_id=qid,
                        max_tokens=OUTPUT_CAPS["judge"],
                    )
                    gate(judge_id, judge, task_id=qid, arm=arm, stage="judge")
                    correct, judge_status = _judge(judge.content, judge.finish_reason)
                    answer_record = read_json(run_root / "private" / "calls" / (answer_id + ".json"))
                    judge_record = read_json(run_root / "private" / "calls" / (judge_id + ".json"))
                    recall, mrr = session_metrics(context["session_ids"], question["evidence_session_ids"])
                    evidence = set(question["evidence_ids"])
                    rows.append(
                        {
                            "question_id": qid,
                            "conversation_id": entry["conversation_id"],
                            "category": question["category"],
                            "question_type": question["question_type"],
                            "arm": arm,
                            "correct": bool(correct and answer_status == "valid" and judge_status == "valid"),
                            "answer_status": answer_status,
                            "judge_status": judge_status,
                            "answer_sha256": sha(answer.content),
                            "judged_answer_sha256": sha(answer.content),
                            "context_sha256": sha(context["context"]),
                            "context_chars": context["context_chars"],
                            "chunk_keys": context["chunk_keys"],
                            "evidence_recall": len(evidence.intersection(context["turn_ids"])) / len(evidence)
                            if evidence
                            else None,
                            "session_recall_at_8": recall,
                            "session_mrr": mrr,
                            "evidence_session_count": question["evidence_session_count"],
                            "answer_usage": answer.usage,
                            "judge_usage": judge.usage,
                            "answer_latency_seconds": answer_record["latency_seconds"],
                            "judge_latency_seconds": judge_record["latency_seconds"],
                        }
                    )
                write_json(run_root / "safe" / (qid + ".json"), {"rows": rows})
        if not gate.verified:
            raise RuntimeError("first two confirmation traces were not remotely verified")
        owner.flush_and_check()
        owner_close = asyncio.run(owner.aclose())
        write_json(run_root / "owner-close.json", owner_close)
        status = {
            "schema_version": 1,
            "status": "completed",
            "protocol": PROTOCOL,
            "campaign_id": plan["campaign_id"],
            "rows": 72,
            "model_calls": EXPECTED_MODEL_CALLS,
            "first_two_cloud_verified": gate.verified,
            "credential_source": credential_source,
            "token_state": token_state(ledger.snapshot()),
            "finished_at": now_utc(),
        }
        write_json(run_root / "status.json", status)
        return status
    except BaseException as error:
        record = {"status": "stopped", "reason": type(error).__name__, "automatic_retry": False, "at": now_utc()}
        try:
            write_json(run_root / "STOPPED.json", record, replace=True)
        except BaseException:
            pass
        try:
            with ledger.transaction() as data:
                data["campaigns"][plan["campaign_id"]]["blocked"] = True
        except BaseException:
            pass
        return record
    finally:
        if owner is not None and owner_close is None:
            try:
                asyncio.run(owner.aclose())
            except BaseException:
                pass


def _mean(rows, key):
    values = [row[key] for row in rows if isinstance(row.get(key), (int, float))]
    return statistics.mean(values) if values else None


def summarize(plan_root, run_root):
    plan_root, run_root = Path(plan_root).resolve(), Path(run_root).resolve()
    plan, preflight_receipt, _ = _verify_ready(plan_root, run_root)
    rows = []
    for path in sorted((run_root / "safe").glob("q_*.json")):
        rows.extend(read_json(path)["rows"])
    if len(rows) != 72 or Counter(row["arm"] for row in rows) != Counter({arm: 24 for arm in ARMS}):
        raise ValueError("confirmation result denominator differs")
    by_arm = {}
    categories = defaultdict(lambda: defaultdict(lambda: {"denominator": 0, "correct": 0}))
    for arm in ARMS:
        subset = [row for row in rows if row["arm"] == arm]
        by_arm[arm] = {
            "denominator": len(subset),
            "correct": sum(bool(row["correct"]) for row in subset),
            "accuracy": sum(bool(row["correct"]) for row in subset) / len(subset),
            "valid_answers": sum(row["answer_status"] == "valid" for row in subset),
            "valid_judges": sum(row["judge_status"] == "valid" for row in subset),
            "mean_session_recall_at_8": _mean(subset, "session_recall_at_8"),
            "mean_session_mrr": _mean(subset, "session_mrr"),
            "mean_evidence_turn_recall_at_8": _mean(subset, "evidence_recall"),
            "answer_usage": usage_sum(subset, "answer_usage"),
            "judge_usage": usage_sum(subset, "judge_usage"),
        }
        for row in subset:
            group = categories[row["question_type"]][arm]
            group["denominator"] += 1
            group["correct"] += int(bool(row["correct"]))
    ledger = TokenLedgerV3(
        plan["ledger_path"],
        campaign_id=plan["campaign_id"],
        max_tokens=plan["budget"]["accounting_units"],
        max_requests=plan["budget"]["max_requests"],
    ).snapshot()
    requests = [row for row in ledger["requests"] if str(row.get("run_id", "")).startswith(CALL_NAMESPACE)]
    if len(requests) != EXPECTED_MODEL_CALLS or any(row.get("status") != "ok" for row in requests):
        raise ValueError("confirmation rows and ledger differ")
    result = {
        "schema_version": 1,
        "protocol": PROTOCOL,
        "status": "complete",
        "selection": plan["selection"],
        "source": plan["source"],
        "gold_sha256": plan["gold"]["sha256"],
        "data": plan["data"],
        "rows": len(rows),
        "anchors": {
            "rows_sha256": sha(rows),
            "plan_sha256": file_sha(plan_root / "plan.json"),
            "preflight_sha256": file_sha(run_root / "PREFLIGHT.json"),
        },
        "arms": by_arm,
        "categories": {name: dict(values) for name, values in sorted(categories.items())},
        "paired_comparisons": paired_comparisons(rows, ARMS, COMPARISONS),
        "requests": request_usage(requests),
        "token_state": token_state(ledger),
        "first_two_readback": read_json(run_root / "first-two-readback.json"),
        "owner_close": read_json(run_root / "owner-close.json"),
        "retrieval_preflight": {
            "questions": preflight_receipt["questions"],
            "arm_questions": preflight_receipt["arm_questions"],
            "max_context_chars": preflight_receipt["max_context_chars"],
            "paid_model_calls": preflight_receipt["paid_model_calls"],
        },
    }
    write_json(run_root / "summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fresh LoCoMo raw-retrieval confirmation study")
    commands = parser.add_subparsers(dest="command", required=True)
    prepare_parser = commands.add_parser("prepare")
    prepare_parser.add_argument("--dataset", required=True)
    prepare_parser.add_argument("--plan", required=True)
    prepare_parser.add_argument("--gold", required=True)
    prepare_parser.add_argument("--run", required=True)
    prepare_parser.add_argument("--ledger", required=True)
    prepare_parser.add_argument("--environment", required=True)
    prepare_parser.add_argument("--model-dir", required=True)
    prepare_parser.add_argument("--campaign-id", required=True)
    prepare_parser.add_argument("--linux-python", default="/home/mll/.cache/my-agent-bench/qwen3-probe-venv/bin/python")
    prepare_parser.add_argument("--cuda-visible-devices", default="0")
    for name in ("preflight", "freeze", "report"):
        subparser = commands.add_parser(name)
        subparser.add_argument("--plan", required=True)
        subparser.add_argument("--run", required=True)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("--plan", required=True)
    run_parser.add_argument("--run", required=True)
    run_parser.add_argument("--use-configured-credential", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "prepare":
        result = prepare(
            args.dataset,
            args.plan,
            args.gold,
            args.run,
            args.ledger,
            args.environment,
            args.model_dir,
            campaign_id=args.campaign_id,
            linux_python=args.linux_python,
            cuda_visible_devices=args.cuda_visible_devices,
        )
    elif args.command == "preflight":
        result = preflight(args.plan, args.run)
    elif args.command == "freeze":
        result = freeze(args.plan, args.run)
    elif args.command == "run":
        result = run(args.plan, args.run, use_configured_credential=args.use_configured_credential)
    else:
        result = summarize(args.plan, args.run)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") not in ("stopped", "failed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
