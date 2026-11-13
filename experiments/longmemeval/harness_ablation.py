"""Frozen 30-question original-English history-index ablation, staged and new-only.

None / production lexical / production Mem0 consume the same deterministic turn
chunks. This does not measure automatic fact extraction or Chinese task quality.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
from uuid import uuid4

from experiments.longmemeval.harness_corpus import (
    ARMS,
    QUESTION_TYPES,
    canonical,
    sha,
    select_records,
    safe_question,
    build_corpus,
    retrieval_metrics,
)


def file_hash(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read(path):
    return json.loads(Path(path).read_bytes())


def write(path, value, *, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace:
        with path.open("xb") as stream:
            stream.write(canonical(value))
        return
    temporary = path.with_name(".write-" + uuid4().hex)
    try:
        write(temporary, value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inventory(root):
    root = Path(root)
    files = [
        {"file": p.relative_to(root).as_posix(), "sha256": file_hash(p), "bytes": p.stat().st_size}
        for p in sorted(root.rglob("*"))
        if p.is_file() and p != root / "manifest.json"
    ]
    write(root / "manifest.json", {"schema_version": 1, "files": files}, replace=True)


def verify_inventory(root):
    root = Path(root).resolve()
    listed = set()
    for proof in read(root / "manifest.json")["files"]:
        path = (root / proof["file"]).resolve()
        if not path.is_relative_to(root) or path.is_symlink() or proof["file"] in listed:
            raise ValueError("artifact inventory escaped root or duplicated")
        if file_hash(path) != proof["sha256"] or path.stat().st_size != proof["bytes"]:
            raise ValueError("artifact checksum mismatch")
        listed.add(proof["file"])
    actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file() and p != root / "manifest.json"}
    if listed != actual:
        raise ValueError("artifact inventory differs")


def tag(qid):
    return sha(qid.encode())[:24]


def implementation():
    repo = Path(__file__).resolve().parents[2]
    paths = [
        "experiments/longmemeval/harness_ablation.py",
        "experiments/longmemeval/harness_corpus.py",
        "experiments/longmemeval/harness_retrieval.py",
        "experiments/longmemeval/harness_paid.py",
        "experiments/benchmark_live/budget.py",
        "experiments/longmemeval/judge.py",
        "src/my_agent_core/memory.py",
        "src/my_agent_core/memory_index.py",
        "src/my_agent_core/memory_mem0.py",
        "src/my_agent_core/memory_local.py",
        "src/my_agent_core/memory_safety.py",
    ]
    return [{"file": name, "sha256": file_hash(repo / name)} for name in paths]


def prepare_plan(
    source,
    output,
    *,
    source_sha256,
    source_revision="unavailable",
    chunk_chars=800,
    budget_chars=6000,
    ledger_path=None,
    campaign_ceiling_usd=None,
):
    source, root = Path(source).resolve(), Path(output).resolve()
    if root.exists():
        raise FileExistsError("plan output must be new")
    if source.name != "longmemeval_s_cleaned.json" or file_hash(source) != source_sha256:
        raise ValueError("expected frozen s_cleaned source SHA")
    if type(budget_chars) is not int or not 1 <= budget_chars <= 6000:
        raise ValueError("context budget must be within 1..6000")
    with source.open(encoding="utf-8") as stream:
        data = json.load(stream)
    selected = select_records(data, seed=23)
    root.mkdir(parents=True, exist_ok=False)
    entries, references = [], {}
    for record in selected:
        question = safe_question(record)
        qid = question["question_id"]
        corpus = build_corpus(record, chunk_chars=chunk_chars)
        qfile, cfile = "questions/" + tag(qid) + ".json", "corpora/" + tag(qid) + ".json"
        write(root / qfile, question)
        write(root / cfile, corpus)
        gold = record.get("answer_session_ids", [])
        if not isinstance(gold, list) or any(not isinstance(s, str) for s in gold):
            raise ValueError("invalid evidence labels")
        references[qid] = {
            "question_type": question["question_type"],
            "answer": record["answer"],
            "answer_session_ids": gold,
            "is_abstention": qid.endswith("_abs") or record.get("has_answer") is False,
        }
        entries.append(
            {
                "question_id": qid,
                "question_type": question["question_type"],
                "question_file": qfile,
                "question_sha256": file_hash(root / qfile),
                "corpus_file": cfile,
                "corpus_sha256": file_hash(root / cfile),
                "chunks": len(corpus),
                "sessions": len(record["haystack_session_ids"]),
            }
        )
    write(root / "references.json", references)  # metrics/judge only; never given to the retrieval worker or solver
    plan = {
        "schema_version": 1,
        "protocol": "fixed-history-index-ablation-v1",
        "language": "original_english",
        "source": {
            "file": str(source),
            "sha256": source_sha256,
            "bytes": source.stat().st_size,
            "revision": source_revision,
            "dataset": "longmemeval_s_cleaned",
        },
        "references_sha256": file_hash(root / "references.json"),
        "seed": 23,
        "per_type": 5,
        "questions": entries,
        "arms": list(ARMS),
        "top_k_chunks": 8,
        "chunk_chars": chunk_chars,
        "budget_chars": budget_chars,
        "model": "deepseek-v4.1-flash",
        "temperature": 0,
        "answer_max_tokens": 1024,
        "judge_max_tokens": 1024,
        "campaign_id": "longhorizon-20261113-memory",
        "campaign_ceiling_usd": campaign_ceiling_usd,
        "ledger_path": str(Path(ledger_path).resolve()) if ledger_path is not None else None,
        "api_run_max_requests": 1,
        "api_run_time_cap": 180,
        "implementation": implementation(),
        "limits": [
            "Fixed history-index ablation, not automatic fact extraction quality.",
            "Original English questions with Chinese BGE embeddings; not Chinese task-quality evidence.",
            "Evidence labels and gold answers are isolated to metrics/judge only.",
            "Non-official DeepSeek judge; no retries or reasoning-text verdict fallback.",
        ],
    }
    write(root / "plan.json", plan)
    if file_hash(source) != source_sha256:
        raise ValueError("source changed while preparing plan")
    inventory(root)
    return plan


def verify_plan(root):
    root = Path(root).resolve()
    verify_inventory(root)
    plan = read(root / "plan.json")
    if (
        plan["protocol"] != "fixed-history-index-ablation-v1"
        or plan["language"] != "original_english"
        or len(plan["questions"]) != 30
        or plan["arms"] != list(ARMS)
        or plan["top_k_chunks"] != 8
        or plan["implementation"] != implementation()
        or file_hash(plan["source"]["file"]) != plan["source"]["sha256"]
    ):
        raise ValueError("frozen source/protocol implementation differs")
    counts = {kind: sum(e["question_type"] == kind for e in plan["questions"]) for kind in QUESTION_TYPES}
    if set(counts.values()) != {5}:
        raise ValueError("frozen sample is not six by five")
    if file_hash(root / "references.json") != plan["references_sha256"]:
        raise ValueError("frozen references differ")
    for entry in plan["questions"]:
        if (
            file_hash(root / entry["question_file"]) != entry["question_sha256"]
            or file_hash(root / entry["corpus_file"]) != entry["corpus_sha256"]
        ):
            raise ValueError("frozen question/corpus differs")
    return plan


def phase_root(plan_root, output, kind, extra, *, resume=False):
    output, plan_root = Path(output).resolve(), Path(plan_root).resolve()
    if output.is_relative_to(plan_root):
        raise ValueError("phase output must be outside immutable plan directory")
    descriptor = {"schema_version": 1, "kind": kind, "plan_sha256": file_hash(plan_root / "plan.json"), **extra}
    if output.exists():
        if not resume:
            raise FileExistsError("phase output must be new; explicit resume required")
        verify_inventory(output)
        if read(output / "phase.json") != descriptor:
            raise ValueError("resume phase differs from frozen inputs")
    else:
        if resume:
            raise ValueError("cannot resume missing phase")
        output.mkdir(parents=True, exist_ok=False)
        write(output / "phase.json", descriptor)
        inventory(output)
    return output


def run_id(plan_sha, qid, arm, stage):
    return f"lme-{plan_sha[:16]}-{tag(qid)}-{arm}-{stage}"


def retrieval_summary(plan_root, output):
    plan = read(Path(plan_root) / "plan.json")
    references = read(Path(plan_root) / "references.json")
    rows = []
    for entry in plan["questions"]:
        path = Path(output) / "results" / (tag(entry["question_id"]) + ".json")
        if path.exists():
            for row in read(path)["rows"]:
                rows.append(
                    {
                        **row,
                        "metrics": retrieval_metrics(
                            row["session_ids"], references[entry["question_id"]], valid=row["valid"]
                        ),
                    }
                )
    arms = {}
    for arm in ARMS:
        values = [r for r in rows if r["arm"] == arm]
        available = [r["metrics"] for r in values if r["metrics"]["reason"] == "available"]
        arms[arm] = {
            "completed_slots": len(values),
            "expected_slots": 30,
            "invalid": sum(not r["valid"] for r in values),
            "metric_coverage": len(available),
            "abstentions": sum(r["metrics"]["reason"] == "abstention_no_evidence" for r in values),
            "mean_recall_at_8": sum(m["recall_at_8"] for m in available) / len(available) if available else None,
            "mrr": sum(m["mrr"] for m in available) / len(available) if available else None,
            "mean_total_seconds": sum(r["total_seconds"] for r in values) / len(values) if values else None,
        }
    return {"status": "completed" if len(rows) == 90 else "partial", "arms": arms, "rows": rows}


def retrieve_phase(
    plan_root,
    output,
    *,
    model_dir,
    index_timeout=1800,
    close_timeout=60,
    threads=2,
    resume=False,
    max_questions=None,
    _worker=None,
):
    if (
        any(not math.isfinite(v) or not 0 < v <= 7200 for v in (index_timeout, close_timeout))
        or type(threads) is not int
        or not 1 <= threads <= 64
        or (max_questions is not None and (type(max_questions) is not int or not 1 <= max_questions <= 30))
    ):
        raise ValueError("invalid retrieval bounds")
    plan_root = Path(plan_root).resolve()
    plan = verify_plan(plan_root)
    root = phase_root(
        plan_root,
        output,
        "retrieval",
        {
            "model_dir": str(Path(model_dir).resolve()),
            "index_timeout": index_timeout,
            "close_timeout": close_timeout,
            "threads": threads,
        },
        resume=resume,
    )
    if not sys.platform.startswith("linux") and _worker is None:
        raise RuntimeError("retrieval CLI requires Linux OS network lockdown")
    processed = 0
    worker = Path(__file__).with_name("harness_retrieval.py")
    try:
        for entry in plan["questions"]:
            qid, identity = entry["question_id"], tag(entry["question_id"])
            result_path, state_path = root / "results" / (identity + ".json"), root / "states" / (identity + ".json")
            if state_path.exists() and read(state_path)["status"] in ("completed", "invalid"):
                continue
            if max_questions is not None and processed >= max_questions:
                break
            processed += 1
            state = {"question_id": qid, "status": "started"}
            interrupted = state_path.exists()
            if not interrupted:
                write(state_path, state)
            result_path.parent.mkdir(exist_ok=True)
            started = time.monotonic()
            try:
                if interrupted:
                    raise RuntimeError("interrupted retrieval is not silently retried")
                command = [
                    sys.executable,
                    "-I",
                    str(worker),
                    "--question",
                    str(plan_root / entry["question_file"]),
                    "--corpus",
                    str(plan_root / entry["corpus_file"]),
                    "--model-dir",
                    str(Path(model_dir).resolve()),
                    "--output",
                    str(result_path),
                    "--budget-chars",
                    str(plan["budget_chars"]),
                    "--index-timeout",
                    str(index_timeout),
                    "--close-timeout",
                    str(close_timeout),
                    "--threads",
                    str(threads),
                ]
                if _worker is None:
                    from my_coding_agent.dream_rsi_replay import _run_worker

                    code = _run_worker(
                        command, cwd=worker.parents[2], timeout_seconds=index_timeout + close_timeout + 180
                    )
                else:
                    code = _worker(command, result_path)
                if code or not result_path.exists():
                    raise RuntimeError("retrieval worker failed")
                value = read(result_path)
                if value["network_boundary"] != "linux_seccomp_no_non_unix_socket_or_connect" or [
                    r["arm"] for r in value["rows"]
                ] != list(ARMS):
                    raise ValueError("retrieval worker protocol differs")
                if any(r["question_id"] != qid or len(r["context"]) > plan["budget_chars"] for r in value["rows"]):
                    raise ValueError("retrieval worker identity/context differs")
                from experiments.longmemeval.harness_corpus import render_chunks

                corpus = read(plan_root / entry["corpus_file"])
                known = {c["key"] for c in corpus}
                for row in value["rows"]:
                    keys = row["chunk_keys"]
                    if (
                        type(row["valid"]) is not bool
                        or len(keys) > 8
                        or len(keys) != len(set(keys))
                        or any(k not in known for k in keys)
                        or (not row["valid"] and (keys or row["context"] or row["session_ids"]))
                    ):
                        raise ValueError("retrieval worker returned invalid corpus identities")
                    rendered = render_chunks(corpus, keys, plan["budget_chars"])
                    if any(row[k] != rendered[k] for k in ("context", "context_chars", "session_ids", "chunk_keys")):
                        raise ValueError("retrieval presentation differs from frozen corpus")
                state["status"] = "completed"
            except Exception as error:
                if result_path.exists():
                    # Quarantine partial evidence; downstream phases see canonical invalid rows.
                    rejected = root / "rejected" / result_path.name
                    rejected.parent.mkdir(exist_ok=True)
                    os.replace(result_path, rejected)
                write(
                    result_path,
                    {
                        "network_boundary": "worker_failed",
                        "rows": [
                            {
                                "question_id": qid,
                                "arm": arm,
                                "valid": False,
                                "context": "",
                                "session_ids": [],
                                "chunk_keys": [],
                                "context_chars": 0,
                                "error_type": type(error).__name__,
                                "total_seconds": time.monotonic() - started,
                            }
                            for arm in ARMS
                        ],
                    },
                )
                state.update(status="invalid", error_type=type(error).__name__)
            write(state_path, {**state, "result_sha256": file_hash(result_path)}, replace=True)
            inventory(root)
        summary = retrieval_summary(plan_root, root)
        write(root / "retrieval-summary.json", summary, replace=True)
        inventory(root)
        return summary
    except BaseException:
        inventory(root)
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--source", required=True, type=Path)
    p.add_argument("--source-sha256", required=True)
    p.add_argument("--source-revision", default="unavailable")
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--ledger", type=Path)
    p.add_argument("--campaign-ceiling-usd", type=float)
    p.add_argument("--chunk-chars", type=int, default=800)
    p.add_argument("--budget-chars", type=int, default=6000)
    p = sub.add_parser("retrieve")
    p.add_argument("--plan", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    p.add_argument("--model-dir", required=True, type=Path)
    p.add_argument("--index-timeout", type=float, default=1800)
    p.add_argument("--close-timeout", type=float, default=60)
    p.add_argument("--threads", type=int, default=2)
    p.add_argument("--max-questions", type=int)
    p.add_argument("--resume", action="store_true")
    for name in ("answer", "judge", "summarize"):
        p = sub.add_parser(name)
        p.add_argument("--plan", required=True, type=Path)
        p.add_argument("--retrieval", required=True, type=Path)
        p.add_argument("--output", required=True, type=Path)
        p.add_argument("--answers", type=Path)
        p.add_argument("--judges", type=Path)
        if name != "summarize":
            p.add_argument("--resume", action="store_true")
            p.add_argument("--max-questions", type=int)
    args = parser.parse_args(argv)
    if args.command == "plan":
        result = prepare_plan(
            args.source,
            args.output,
            source_sha256=args.source_sha256,
            source_revision=args.source_revision,
            chunk_chars=args.chunk_chars,
            budget_chars=args.budget_chars,
            ledger_path=args.ledger,
            campaign_ceiling_usd=args.campaign_ceiling_usd,
        )
        print(json.dumps({"status": "planned", "questions": len(result["questions"])}))
    elif args.command == "retrieve":
        result = retrieve_phase(
            args.plan,
            args.output,
            model_dir=args.model_dir,
            index_timeout=args.index_timeout,
            close_timeout=args.close_timeout,
            threads=args.threads,
            max_questions=args.max_questions,
            resume=args.resume,
        )
        print(json.dumps({"status": result["status"], "arms": result["arms"]}))
    else:
        from experiments.longmemeval.harness_paid import paid_phase, summarize

        if args.command == "summarize":
            result = summarize(args.plan, args.retrieval, args.answers, args.judges, args.output)
        else:
            result = asyncio.run(
                paid_phase(
                    args.command,
                    args.plan,
                    args.retrieval,
                    args.output,
                    answers=args.answers,
                    resume=args.resume,
                    max_questions=args.max_questions,
                )
            )
        print(json.dumps(result))
    return 2 if result.get("status") in ("stopped", "failed") else 0


if __name__ == "__main__":
    raise SystemExit(main())
