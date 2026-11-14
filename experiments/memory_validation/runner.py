"""New-only 12-question validation; preparation never starts a model.

Reuses production memory workers, guarded live LLM and durable ledger. Retrieval
is Linux/seccomp-only. Gold material stays in the host plan, outside worker input.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
from pathlib import Path
import sys
import time

if __package__ in (None, ""):
    _root = Path(__file__).resolve().parents[2]
    sys.path[:0] = [str(_root / "src"), str(_root / "my-eval/src"), str(_root)]

from filelock import FileLock

from experiments.benchmark_live.budget import BudgetLedger, BudgetStop, GuardedLLM, create_live_llm
from experiments.longmemeval.harness_ablation import (
    file_hash,
    implementation as prior_implementation,
    inventory,
    phase_root,
    read,
    run_id,
    tag,
    verify_inventory,
    write,
)
from experiments.longmemeval.harness_corpus import (
    ARMS,
    QUESTION_TYPES,
    answer_prompt,
    build_corpus,
    canonical,
    render_chunks,
    retrieval_metrics,
    safe_question,
    session_alias,
    sha,
)
from experiments.longmemeval.harness_paid import judge_prompt

SOURCE_SHA256 = "d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442"
PROTOCOL = "memory-effect-v1-disjoint12-bge-cpu"


def local_path(value):
    """Locate the same frozen bytes through Windows/WSL drive mounts."""
    value = str(value).replace("\\", "/")
    if sys.platform.startswith("linux") and len(value) > 2 and value[1:3] == ":/":
        return Path("/mnt/" + value[0].lower() + "/" + value[3:])
    if (
        sys.platform == "win32"
        and value.startswith("/mnt/")
        and len(value) > 6
        and value[5].isalpha()
        and value[6] == "/"
    ):
        return Path(value[5].upper() + ":/" + value[7:])
    return Path(value)


def select_new(records, excluded, seed=24):
    """Choose by identity/type only; never inspect answers or prior performance."""
    groups = {kind: [] for kind in QUESTION_TYPES}
    seen = set()
    for row in records:
        qid, kind = row.get("question_id"), row.get("question_type")
        if not isinstance(qid, str) or not qid or qid in seen or kind not in groups:
            raise ValueError("invalid/duplicate question identity or type")
        seen.add(qid)
        if qid not in excluded:
            groups[kind].append(row)
    rng, selected = random.Random(seed), []
    for kind in sorted(groups):
        group = sorted(groups[kind], key=lambda r: r["question_id"])
        abstentions = [r for r in group if r["question_id"].endswith("_abs")]
        ordinary = [r for r in group if not r["question_id"].endswith("_abs")]
        if len(group) < 2 or abstentions and not ordinary:
            raise ValueError("insufficient disjoint questions per type")
        first = [rng.choice(abstentions)] if abstentions else []
        selected.extend(first + rng.sample(ordinary if first else group, 2 - len(first)))
    return sorted(selected, key=lambda r: (r["question_type"], r["question_id"]))


def implementation():
    root = Path(__file__).resolve().parents[2]
    extra = ["experiments/memory_validation/runner.py", "src/my_agent_llm/client.py"]
    return prior_implementation() + [{"file": p, "sha256": file_hash(root / p)} for p in extra]


def exclusion_ids(rows):
    ids = {r["question_id"] for r in rows}
    if len(ids) != 30 or any(
        sum(1 for qid in ids if any(r["question_id"] == qid and r["question_type"] == kind for r in rows)) != 5
        for kind in QUESTION_TYPES
    ):
        raise ValueError("exclusion source must identify the old six-by-five sample")
    return ids


def prepare(source, output, *, old_rows, model_dir, source_revision, source_license, ledger_path, campaign_id):
    source, root, old_rows, model_dir = map(lambda p: Path(p).resolve(), (source, output, old_rows, model_dir))
    if root.exists() or source.name != "longmemeval_s_cleaned.json" or file_hash(source) != SOURCE_SHA256:
        raise ValueError("new output and exact frozen LongMemEval source required")
    if not source_revision or not source_license or not campaign_id or not Path(ledger_path).is_absolute():
        raise ValueError("explicit provenance, campaign and absolute ledger path required")
    ledger = Path(ledger_path).resolve()
    for suffix in ("", ".lock", ".tokens.lock", ".study.lock"):
        mutable = Path(str(ledger) + suffix).resolve()
        if mutable == root or root in mutable.parents or mutable in root.parents:
            raise ValueError("mutable ledger and lock paths must not overlap the immutable plan")
    from my_agent_core.memory_local import verify_model_files

    model = verify_model_files(model_dir)  # checksum only, no embedding runtime
    excluded = exclusion_ids(read(old_rows))
    selected = select_new(read(source), excluded)
    root.mkdir(parents=True)
    entries, references = [], {}
    for record in selected:
        question = safe_question(record)
        qid = question["question_id"]
        identity = tag(qid)
        corpus = build_corpus(record, chunk_chars=800)
        qfile, cfile = f"questions/{identity}.json", f"corpora/{identity}.json"
        write(root / qfile, question)
        write(root / cfile, corpus)
        gold = record["answer_session_ids"]
        if not isinstance(gold, list) or any(not isinstance(s, str) for s in gold):
            raise ValueError("invalid evidence labels")
        references[qid] = {
            "question_type": question["question_type"],
            "answer": record["answer"],
            "answer_session_ids": gold,
            "answer_session_aliases": [session_alias(s) for s in gold],
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
    write(root / "references.json", references)
    plan = {
        "schema_version": 1,
        "protocol": PROTOCOL,
        "seed": 24,
        "per_type": 2,
        "questions": entries,
        "source": {
            "file": str(source),
            "sha256": SOURCE_SHA256,
            "bytes": source.stat().st_size,
            "revision": source_revision,
            "license": source_license,
            "language": "original_english",
        },
        "exclusions": {"file": str(old_rows), "sha256": file_hash(old_rows), "question_ids": sorted(excluded)},
        "references_sha256": file_hash(root / "references.json"),
        "arms": list(ARMS),
        "top_k_chunks": 8,
        "chunk_chars": 800,
        "budget_chars": 6000,
        "fallback_policy": "semantic errors invalid; legitimate empty zero; never substitute lexical",
        "embedding": {
            "profile": "local_cpu_zh",
            "model_dir": str(model_dir),
            "model_manifest_sha256": file_hash(model_dir / "manifest.json"),
            "manifest": model,
            "threads": 2,
            "index_timeout": 1800,
            "close_timeout": 60,
        },
        "model": "deepseek-v4.1-flash",
        "provider": "openai",
        "base_url": "https://opencode.ai/inference/go/openai/v1",
        "temperature": 0,
        "max_output_tokens": 1024,
        "timeout_seconds": 180,
        "max_retries": 0,
        "campaign_id": campaign_id,
        "ledger_path": str(ledger),
        "limits": {"requests": 72, "tokens": 3000000},
        "cost_usd": None,
        "legacy_ledger_policy": "30 USD proxy compatibility ceiling is not new spend authorization or a supplier bill; token ceiling governs first",
        "judge": "official prompt with nonofficial DeepSeek; exact yes/no only; malformed unavailable; no retries",
        "judge_blinding": "fixed question/arm schedule; judge prompt sees question, reference, hypothesis only, never arm or retrieval context",
        "selection": "exclude old 30, six types each 2; seed24; one suffix_abs per type when present; remaining ordinary; no outcomes used",
        "implementation": implementation(),
        "limitations": [
            "fixed history chunks, no automatic extraction or consolidation quality",
            "Chinese BGE on English history; no universal Mem0 ranking",
            "12 questions, one draw/arm, no statistical superiority claim",
        ],
    }
    write(root / "plan.json", plan)
    inventory(root)
    if file_hash(source) != SOURCE_SHA256:
        raise ValueError("source changed")
    return plan


def verify_plan(root):
    root = Path(root).resolve()
    verify_inventory(root)
    p = read(root / "plan.json")
    if (
        p["protocol"] != PROTOCOL
        or len(p["questions"]) != 12
        or p["arms"] != list(ARMS)
        or p["implementation"] != implementation()
        or p["limits"] != {"requests": 72, "tokens": 3000000}
        or p["model"] != "deepseek-v4.1-flash"
        or p["max_retries"] != 0
        or p["budget_chars"] != 6000
        or p["top_k_chunks"] != 8
        or p["max_output_tokens"] != 1024
        or p["timeout_seconds"] != 180
        or p["embedding"]["profile"] != "local_cpu_zh"
        or p["source"]["sha256"] != SOURCE_SHA256
    ):
        raise ValueError("frozen protocol differs")
    if any(sum(e["question_type"] == kind for e in p["questions"]) != 2 for kind in QUESTION_TYPES):
        raise ValueError("not six types by two")
    ids = [e["question_id"] for e in p["questions"]]
    if len(set(ids)) != 12 or set(ids) & set(p["exclusions"]["question_ids"]):
        raise ValueError("sample overlap")
    for section in ("source", "exclusions"):
        if file_hash(local_path(p[section]["file"])) != p[section]["sha256"]:
            raise ValueError("source/exclusions changed")
    if file_hash(root / "references.json") != p["references_sha256"]:
        raise ValueError("references changed")
    if file_hash(local_path(p["embedding"]["model_dir"]) / "manifest.json") != p["embedding"]["model_manifest_sha256"]:
        raise ValueError("embedding manifest changed")
    from my_agent_core.memory_local import verify_model_files

    verify_model_files(local_path(p["embedding"]["model_dir"]))
    for e in p["questions"]:
        for name in ("question", "corpus"):
            if file_hash(root / e[name + "_file"]) != e[name + "_sha256"]:
                raise ValueError("frozen input changed")
    return p


def token_state(data):
    used, held, unknown = 0, 0, False
    for r in data["requests"]:
        u = r.get("usage") or {}
        total = u.get("total_tokens")
        completion = u.get("completion_tokens")
        prompt = u.get("prompt_tokens")
        if (
            r["status"] == "ok"
            and type(total) is int
            and type(completion) is int
            and type(prompt) is int
            and completion >= 0
            and prompt >= 0
            and total >= completion + prompt
        ):
            used += total
        else:
            unknown = True
            held += r["input_token_bound"] + r["max_output_tokens"]
    return {"requests": len(data["requests"]), "used_tokens": used, "held_tokens": held, "unknown_or_inflight": unknown}


class TokenLedger(BudgetLedger):
    """Add global request/token bounds to the existing durable production ledger."""

    def __init__(self, path, *, campaign_id, max_tokens=3000000, max_requests=72):
        super().__init__(path, campaign_id=campaign_id, limit_usd="30", spend_ceiling_usd=30)
        self.study_tokens, self.study_requests = max_tokens, max_requests
        self.study_lock = FileLock(str(self.path) + ".tokens.lock", timeout=10)

    def reserve(self, run_id, *, input_bound, max_output, model, provider, purpose):
        with self.study_lock:
            s = token_state(self.snapshot())
            if (
                s["unknown_or_inflight"]
                or s["requests"] >= self.study_requests
                or s["used_tokens"] + input_bound + max_output > self.study_tokens
            ):
                raise BudgetStop("study token/request limit or unknown usage")
            return super().reserve(
                run_id, input_bound=input_bound, max_output=max_output, model=model, provider=provider, purpose=purpose
            )

    def settle(self, request_id, *, usage, status):
        with self.study_lock:
            ok = super().settle(request_id, usage=usage, status=status)
            s = token_state(self.snapshot())
            if s["unknown_or_inflight"] or s["used_tokens"] > self.study_tokens:
                with self.transaction() as data:
                    data["campaigns"][self.campaign_id]["blocked"] = True
                return False
            return ok


def verify_retrieval(entry, rows, corpus, budget):
    if [r["arm"] for r in rows] != list(ARMS):
        raise ValueError("arms differ")
    for row in rows:
        keys = row["chunk_keys"]
        if (
            row["question_id"] != entry["question_id"]
            or type(row["valid"]) is not bool
            or len(keys) > 8
            or len(keys) != len(set(keys))
            or row["arm"] == "none"
            and keys
            or not row["valid"]
            and (keys or row["context"] or row["session_ids"])
        ):
            raise ValueError("retrieval identity differs")
        rendered = render_chunks(corpus, keys, budget)
        if any(row[k] != rendered[k] for k in ("context", "context_chars", "session_ids", "chunk_keys")):
            raise ValueError("retrieval text does not match frozen corpus")


def verify_phase(plan_root, output, kind, *, allow_stopped=False):
    output = Path(output).resolve()
    verify_inventory(output)
    d = read(output / "phase.json")
    if d["kind"] != kind or d["plan_sha256"] != file_hash(Path(plan_root) / "plan.json"):
        raise ValueError("phase differs")
    if not allow_stopped and (output / "STOPPED.json").exists():
        raise BudgetStop("input phase stopped; no new paid work")
    return output


def retrieve(plan_root, output, *, max_questions=None, resume=False):
    """Run existing label-free, network-denied worker serially; never in Windows."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("retrieval requires Linux seccomp worker")
    plan_root = Path(plan_root).resolve()
    p = verify_plan(plan_root)
    root = phase_root(plan_root, output, "retrieval", {"embedding": p["embedding"]}, resume=resume)
    worker = Path(__file__).resolve().parents[1] / "longmemeval/harness_retrieval.py"
    from my_coding_agent.dream_rsi_replay import _run_worker

    with FileLock(str(root) + ".execution.lock", timeout=0):
        if (root / "STOPPED.json").exists():
            return read(root / "STOPPED.json")
        processed = 0
        for e in p["questions"]:
            identity = tag(e["question_id"])
            state = root / "states" / f"{identity}.json"
            result = root / "results" / f"{identity}.json"
            if state.exists():
                if read(state)["status"] == "completed":
                    continue
                write(root / "STOPPED.json", {"status": "stopped", "reason": "interrupted_retrieval_no_retry"})
                inventory(root)
                return read(root / "STOPPED.json")
            if max_questions is not None and processed >= max_questions:
                break
            processed += 1
            write(state, {"status": "started", "question_id": e["question_id"]})
            # Only question/corpus files are passed. Never pass references or gold.
            result.parent.mkdir(exist_ok=True)
            command = [
                sys.executable,
                "-I",
                str(worker),
                "--question",
                str(plan_root / e["question_file"]),
                "--corpus",
                str(plan_root / e["corpus_file"]),
                "--model-dir",
                str(local_path(p["embedding"]["model_dir"])),
                "--output",
                str(result),
                "--budget-chars",
                "6000",
                "--threads",
                "2",
                "--index-timeout",
                "1800",
                "--close-timeout",
                "60",
            ]
            try:
                code = _run_worker(command, cwd=worker.parents[2], timeout_seconds=2040)
                if code or not result.exists():
                    raise RuntimeError("retrieval worker failed")
                value = read(result)
                if value["network_boundary"] != "linux_seccomp_no_non_unix_socket_or_connect":
                    raise ValueError("network boundary differs")
                verify_retrieval(e, value["rows"], read(plan_root / e["corpus_file"]), 6000)
                if any(not r["valid"] or not r["close_complete"] for r in value["rows"]):
                    raise ValueError("invalid retrieval")
                write(state, {"status": "completed", "result_sha256": file_hash(result)}, replace=True)
            except BaseException as exc:
                write(root / "STOPPED.json", {"status": "stopped", "reason": type(exc).__name__})
                inventory(root)
                raise
            inventory(root)
    return {"status": "completed" if len(list((root / "results").glob("*.json"))) == 12 else "partial"}


async def paid(stage, plan_root, retrieval, output, *, answers=None, resume=False, max_questions=None):
    from my_agent_llm.models import Message
    from my_agent_llm.observation import observation_context
    from my_coding_agent.research_cli import close_owned_llm

    if stage not in ("answer", "judge"):
        raise ValueError("unknown paid stage")
    plan_root = Path(plan_root).resolve()
    p = verify_plan(plan_root)
    retrieval = verify_phase(plan_root, retrieval, "retrieval")
    if stage == "judge":
        answers = verify_phase(plan_root, answers, "answer")
        if read(answers / "phase.json")["retrieval_phase_sha256"] != file_hash(retrieval / "phase.json"):
            raise ValueError("answer phase belongs to different retrieval")
    descriptor = {"retrieval_phase_sha256": file_hash(retrieval / "phase.json")}
    if answers is not None:
        descriptor["answer_phase_sha256"] = file_hash(answers / "phase.json")
    root = phase_root(plan_root, output, stage, descriptor, resume=resume)
    if not local_path(p["ledger_path"]).exists():
        # Ledger belongs to this new study, never an implicit reset of another study.
        local_path(p["ledger_path"]).parent.mkdir(parents=True, exist_ok=True)
    ledger = TokenLedger(local_path(p["ledger_path"]), campaign_id=p["campaign_id"])
    references = read(plan_root / "references.json") if stage == "judge" else None
    raw = None
    with FileLock(str(local_path(p["ledger_path"])) + ".study.lock", timeout=0):
        if (root / "STOPPED.json").exists():
            return read(root / "STOPPED.json")
        try:
            if ledger.campaign_status().get("blocked") or token_state(ledger.snapshot())["unknown_or_inflight"]:
                raise BudgetStop("whole study is blocked or in flight")
            count = 0
            for e in p["questions"]:
                terminal_paths = [root / "trials" / f"{tag(e['question_id'])}-{arm}.json" for arm in ARMS]
                terminal = all(
                    path.exists() and read(path)["status"] in ("completed", "unavailable") for path in terminal_paths
                )
                if max_questions is not None and count >= max_questions and not terminal:
                    break
                identity = tag(e["question_id"])
                rpath = retrieval / "results" / f"{identity}.json"
                if not rpath.exists():
                    break
                rows = read(rpath)["rows"]
                verify_retrieval(e, rows, read(plan_root / e["corpus_file"]), 6000)
                question = read(plan_root / e["question_file"])
                if not terminal:
                    count += 1
                for arm in ARMS:
                    trial = root / "trials" / f"{identity}-{arm}.json"
                    if trial.exists():
                        old = read(trial)
                        if (
                            old.get("question_id") != e["question_id"]
                            or old.get("arm") != arm
                            or old.get("stage") != stage
                            or old["retrieval_sha256"] != file_hash(rpath)
                        ):
                            raise ValueError("used retrieval changed")
                        if stage == "judge" and "answer_sha256" in old:
                            apath = answers / "trials" / f"{identity}-{arm}.json"
                            if not apath.exists() or file_hash(apath) != old["answer_sha256"]:
                                raise ValueError("used answer changed")
                        if old["status"] in ("completed", "unavailable"):
                            continue
                        raise BudgetStop("interrupted trial cannot be retried")
                    row = next(r for r in rows if r["arm"] == arm)
                    if not row["valid"]:
                        raise BudgetStop("invalid retrieval stops study")
                    state = {
                        "question_id": e["question_id"],
                        "question_type": e["question_type"],
                        "arm": arm,
                        "stage": stage,
                        "status": "started",
                        "retrieval_sha256": file_hash(rpath),
                    }
                    if stage == "answer":
                        messages = answer_prompt(question, row["context"])
                    else:
                        apath = answers / "trials" / f"{identity}-{arm}.json"
                        if not apath.exists():
                            continue
                        a = read(apath)
                        if (
                            a.get("question_id") != e["question_id"]
                            or a.get("arm") != arm
                            or a.get("stage") != "answer"
                            or a.get("retrieval_sha256") != file_hash(rpath)
                        ):
                            raise ValueError("answer identity or retrieval differs before judge")
                        state["answer_sha256"] = file_hash(apath)
                        if a["status"] != "completed":
                            write(trial, {**state, "status": "unavailable", "reason": "answer_unavailable"})
                            inventory(root)
                            continue
                        messages = [
                            {
                                "role": "user",
                                "content": judge_prompt(question, references[e["question_id"]], a["hypothesis"]),
                            }
                        ]
                    identifier = run_id(file_hash(plan_root / "plan.json"), e["question_id"], arm, stage)
                    ledger.register_run(identifier, max_requests=1, time_cap=180)
                    state.update(run_id=identifier, input_sha256=sha(canonical(messages)))
                    write(trial, state)
                    inventory(root)
                    if raw is None:
                        raw = create_live_llm(max_output_tokens=1024, timeout=180)
                    if (
                        raw.model != p["model"]
                        or raw.config.provider != p["provider"]
                        or raw.config.max_retries != 0
                        or raw.config.base_url != p["base_url"]
                        or raw.config.max_tokens != 1024
                        or raw.config.timeout != 180
                    ):
                        raise ValueError("live model settings differ")
                    started = time.monotonic()
                    with observation_context(root_id=identifier, purpose="memory_validation_" + stage):
                        response = await GuardedLLM(raw, ledger, identifier).achat(
                            [Message(**m) for m in messages], max_tokens=1024, temperature=0
                        )
                    content = response.content.strip()
                    state.update(
                        usage=response.usage,
                        finish_reason=response.finish_reason,
                        latency_seconds=time.monotonic() - started,
                    )
                    if stage == "answer":
                        state.update(
                            status="completed" if content else "unavailable",
                            hypothesis=content,
                            reason=None if content else "empty_answer",
                        )
                    else:
                        state.update(
                            status="completed" if content.lower() in ("yes", "no") else "unavailable",
                            correct=(content.lower() == "yes") if content.lower() in ("yes", "no") else None,
                            verdict=content,
                            judge_level="official_prompt_nonofficial_deepseek_model",
                            reason=None if content.lower() in ("yes", "no") else "invalid_judge_format",
                        )
                    write(trial, state, replace=True)
                    inventory(root)
        except BaseException as exc:
            write(root / "STOPPED.json", {"status": "stopped", "reason": type(exc).__name__}, replace=True)
            inventory(root)
            raise
        finally:
            if raw is not None:
                await close_owned_llm(raw)
    return {
        "status": "completed" if len(list((root / "trials").glob("*.json"))) == 36 else "partial",
        **token_state(ledger.snapshot()),
    }


def summarize(plan_root, retrieval, answers, judges):
    plan_root = Path(plan_root).resolve()
    p = verify_plan(plan_root)
    retrieval, answers, judges = [
        verify_phase(plan_root, o, k, allow_stopped=True)
        for o, k in ((retrieval, "retrieval"), (answers, "answer"), (judges, "judge"))
    ]
    refs = read(plan_root / "references.json")
    rows = []
    for e in p["questions"]:
        identity = tag(e["question_id"])
        rpath = retrieval / "results" / f"{identity}.json"
        if not rpath.exists():
            continue
        retrieved = read(rpath)["rows"]
        verify_retrieval(e, retrieved, read(plan_root / e["corpus_file"]), 6000)
        for r in retrieved:
            ap = answers / "trials" / f"{identity}-{r['arm']}.json"
            jp = judges / "trials" / f"{identity}-{r['arm']}.json"
            a = read(ap) if ap.exists() else {}
            j = read(jp) if jp.exists() else {}
            if a and a["retrieval_sha256"] != file_hash(rpath):
                raise ValueError("answer retrieval changed")
            if j and (j["retrieval_sha256"] != file_hash(rpath) or j.get("answer_sha256") != file_hash(ap)):
                raise ValueError("judged input changed")
            rows.append(
                {
                    "question_id": e["question_id"],
                    "question_type": e["question_type"],
                    "arm": r["arm"],
                    "retrieval_valid": r["valid"],
                    **retrieval_metrics(r["session_ids"], refs[e["question_id"]], valid=r["valid"]),
                    "context_chars": r["context_chars"],
                    "retrieval_seconds": r["total_seconds"],
                    "correct": j.get("correct"),
                    "answer_status": a.get("status", "pending"),
                    "judge_status": j.get("status", "pending"),
                    "answer_finish_reason": a.get("finish_reason"),
                    "judge_finish_reason": j.get("finish_reason"),
                }
            )
    arms = {}
    for arm in ARMS:
        group = [r for r in rows if r["arm"] == arm]
        available = [r for r in group if r["correct"] is not None]
        arms[arm] = {
            "expected": 12,
            "available": len(available),
            "correct": sum(bool(r["correct"]) for r in available),
            "pending_or_unavailable": 12 - len(available),
            "answer_length": sum(r["answer_finish_reason"] == "length" for r in group),
            "judge_length": sum(r["judge_finish_reason"] == "length" for r in group),
        }
    return {
        "protocol": PROTOCOL,
        "arms": arms,
        "rows": rows,
        "cost_usd": None,
        "ledger": token_state(read(local_path(p["ledger_path"]))),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare")
    for key in (
        "source",
        "output",
        "old-rows",
        "model-dir",
        "source-revision",
        "source-license",
        "ledger-path",
        "campaign-id",
    ):
        prep.add_argument("--" + key, required=True)
    check = sub.add_parser("verify")
    check.add_argument("--plan", required=True)
    for stage in ("retrieve", "answer", "judge", "summarize"):
        cmd = sub.add_parser(stage)
        cmd.add_argument("--plan", required=True)
        if stage != "retrieve":
            cmd.add_argument("--retrieval", required=True)
        if stage in ("judge", "summarize"):
            cmd.add_argument("--answers", required=True)
        if stage == "summarize":
            cmd.add_argument("--judges", required=True)
        else:
            cmd.add_argument("--output", required=True)
            cmd.add_argument("--resume", action="store_true")
            cmd.add_argument("--max-questions", type=int, choices=range(1, 13))
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    if command == "prepare":
        p = prepare(**args)
        result = {
            "status": "prepared",
            "questions": [{k: e[k] for k in ("question_id", "question_type")} for e in p["questions"]],
            "plan_sha256": file_hash(Path(args["output"]) / "plan.json"),
        }
    elif command == "verify":
        p = verify_plan(args["plan"])
        result = {"valid": True, "questions": len(p["questions"]), "limits": p["limits"]}
    else:
        args["plan_root"] = args.pop("plan")
        result = (
            retrieve(**args)
            if command == "retrieve"
            else summarize(**args)
            if command == "summarize"
            else asyncio.run(paid(command, **args))
        )
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
