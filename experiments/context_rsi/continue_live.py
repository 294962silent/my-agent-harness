"""One registered operational exception; frozen execution code stays unchanged.

The original failed slot and summary.blocked remain unchanged. This controller
never calls run_batch, retries a started slot, repairs evidence or selects policy.
PermissionError's cause remains unlocalized; this is not an environment-fix claim.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
from uuid import uuid4

from filelock import FileLock

from experiments.context_rsi import live_runner as runner

INCIDENT_ID = "permissionerror-operational-exception-r1"
FAILED_SLOT = "test-HumanEval-6-s23-C"
_SAFE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}")


class ContinuationError(RuntimeError):
    """The exact reviewed exception no longer permits further dispatch."""


def _require(condition, message):
    if not condition:
        raise ContinuationError(message)


def _unique(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate incident JSON key")
        result[key] = value
    return result


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=_unique)


def _hash(path):
    return runner._sha(Path(path).read_bytes())


def _script_hash():
    return _hash(Path(__file__))


def _separate(output, *inputs):
    _require(not output.exists() and all(not output.is_relative_to(p) and not p.is_relative_to(output) for p in inputs),
             "output must be new and separate from campaign and incident")


def _reviewed(campaign):
    campaign.assert_execution_pin()
    plan, _ = runner.verify_plan(campaign)
    rows = campaign.events()
    trials, requests = campaign._state(rows)
    _require(campaign.manifest["campaign_id"] == "rsi-live-20261114-v2", "wrong reviewed campaign")
    _require(not runner.Campaign._blocked(requests), "pending, unknown or violated request blocks continuation")
    failed = {name for name, item in trials.items() if item["status"] != "completed"}
    _require(failed == {FAILED_SLOT} and trials[FAILED_SLOT]["status"] == "failed", "unreviewed failure or started trial")
    pending = [s["trial_id"] for s in plan["slots"] if s["trial_id"] not in trials]
    _require(len(plan["slots"]) == 53 and len(trials) == 16 and len(pending) == 37, "reviewed 16/37 inventory changed")
    _require(all(s["phase"] == "test" for s in plan["slots"] if s["trial_id"] in pending), "collection cannot be repeated")
    _require(not any(r["event"] == "operator_continuation" for r in rows), "continuation already registered")
    terminal = [r for r in rows if r["event"] == "trial_finished" and r["trial_id"] == FAILED_SLOT]
    _require(len(terminal) == 1 and terminal[0]["status"] == "failed", "failed terminal differs")
    directory = campaign.root / "trials" / FAILED_SLOT
    final_path, report_path = directory / "final-evaluation.json", directory / "result.json"
    final, report = _read(final_path), _read(report_path)
    _require(terminal[0]["evidence"]["final_evaluation_sha256"] == _hash(final_path)
             and final["runtime_result_sha256"] == _hash(report_path), "failed result seal mismatch")
    _require(report["status"] == "invalid" and report["error_type"] == "PermissionError"
             and report["cleanup_errors"] == [] and report["budget"]["usage_complete"] is True
             and report["budget"]["blocked"] is False and report["budget"]["held_tokens"] == 0
             and final["final"] is None and final["final_success"] is False, "incident is not the exact reviewed failure")
    policy_path = campaign.root / "research-policy.json"
    policy = _read(policy_path)
    finished = [r for r in rows if r["event"] == "optimizer_finished"]
    _require(len(finished) == 1 and finished[0]["policy"] == policy
             and finished[0]["policy_sha256"] == runner._sha(runner._bytes(policy))
             and policy.get("promoted") is False, "committed policy differs")
    pins = {"campaign.json": _hash(campaign.manifest_path), "plan.json": _hash(campaign.root / "plan.json"),
            "research-policy.json": _hash(policy_path),
            f"trials/{FAILED_SLOT}/result.json": _hash(report_path),
            f"trials/{FAILED_SLOT}/final-evaluation.json": _hash(final_path)}
    return {"schema_version": 1, "incident_id": INCIDENT_ID, "campaign_root": str(campaign.root),
        "script_sha256": _script_hash(), "original_event_count": len(rows), "original_journal_sha256": _hash(campaign.path),
        "original_journal_head": rows[-1]["sha256"], "failed_slot": FAILED_SLOT,
        "failed_terminal_sha256": terminal[0]["sha256"], "original_started_ids": sorted(trials),
        "pending_trial_ids": pending, "pending_ids_sha256": runner._sha(runner._bytes(pending)), "pins": pins,
        "source_pin_sha256": runner._sha(runner._bytes(campaign.manifest["execution_source"])),
        "task_manifest": plan["task_manifest"], "task_manifest_sha256": plan["task_manifest_sha256"],
        "original_requests": len(requests), "original_known_tokens": sum(r.get("used_tokens") or 0 for r in requests.values()),
        "decision": "explicit operational exception after an unlocalized PermissionError; not a root-cause fix",
        "failure_policy": "never retry any started slot; any additional failure or unsettled usage stops"}


def register(campaign_root, output):
    """Zero-provider registration. Does not alter existing events or paid results."""
    campaign, output = runner.Campaign(campaign_root), Path(output).resolve()
    _separate(output, campaign.root)
    incident = _reviewed(campaign)
    output.mkdir(parents=True, exist_ok=False)
    runner._exclusive_json(output / "incident.json", incident)
    with (output / "original-journal.jsonl").open("xb") as stream:
        stream.write(campaign.path.read_bytes())
        stream.flush()
        os.fsync(stream.fileno())
    seal = {"schema_version": 1, "files": {name: _hash(output / name) for name in ("incident.json", "original-journal.jsonl")}}
    runner._exclusive_json(output / "seal.json", seal)
    runner._sync_directory(output)
    runner._sync_directory(output.parent)
    def check(trials, requests):
        _require(_hash(campaign.path) == incident["original_journal_sha256"]
                 and not runner.Campaign._blocked(requests), "campaign changed before registration")
    campaign._append("operator_continuation", check=check, phase="registered", incident_id=INCIDENT_ID,
        incident_seal_sha256=_hash(output / "seal.json"), script_sha256=incident["script_sha256"],
        pending_ids_sha256=incident["pending_ids_sha256"], incident_directory=str(output))
    return incident


class ContinuationCampaign(runner.Campaign):
    """Only start_trial changes: an exact CAS exception, never a state rewrite."""
    def __init__(self, root, incident_directory):
        super().__init__(root)
        self.incident_directory = Path(incident_directory).resolve()
        self._active_trial = None
        self.incident = _read(self.incident_directory / "incident.json")
        self._seal_sha256 = _hash(self.incident_directory / "seal.json")
        self.assert_execution_pin()

    def _static(self):
        seal = _read(self.incident_directory / "seal.json")
        _require(_hash(self.incident_directory / "seal.json") == self._seal_sha256
                 and seal["schema_version"] == 1, "incident seal changed")
        _require(set(seal["files"]) == {"incident.json", "original-journal.jsonl"}
                 and all(_hash(self.incident_directory / name) == digest for name, digest in seal["files"].items()),
                 "incident bytes changed")
        _require(self.incident == _read(self.incident_directory / "incident.json"), "in-memory incident differs from sealed bytes")
        i = self.incident
        _require(i["script_sha256"] == _script_hash() and i["campaign_root"] == str(self.root)
                 and i["incident_id"] == INCIDENT_ID and i["failed_slot"] == FAILED_SLOT, "wrapper or incident identity changed")
        _require(all(_hash(self.root / name) == digest for name, digest in i["pins"].items()), "reviewed source/policy/failure artifact changed")
        _require(_hash(i["task_manifest"]) == i["task_manifest_sha256"], "task manifest changed")
        before = (self.incident_directory / "original-journal.jsonl").read_bytes()
        _require(runner._sha(before) == i["original_journal_sha256"] and self.path.read_bytes().startswith(before),
                 "original journal prefix changed")
        tail = self._read()[i["original_event_count"]:]
        receipts = [r for r in tail if r["event"] == "operator_continuation" and r.get("phase") == "registered"]
        _require(len(receipts) == 1 and receipts[0]["incident_seal_sha256"] == self._seal_sha256
                 and receipts[0]["script_sha256"] == i["script_sha256"]
                 and receipts[0]["pending_ids_sha256"] == i["pending_ids_sha256"], "registration receipt differs")
        for row in tail:
            if row["event"] == "operator_continuation":
                _require(row.get("incident_id") == INCIDENT_ID and row.get("incident_seal_sha256") == self._seal_sha256,
                         "additional operational exception is forbidden")
            else:
                _require(row["event"] in ("trial_started", "trial_finished", "request_reserved", "request_settled", "budget_stopped")
                         and row.get("trial_id") in i["pending_trial_ids"], "activity outside the registered pending slots")

    def _gate(self, rows, *, allow_active=None):
        trials, requests = self._state(rows)
        _require(not self._blocked(requests), "pending/unknown/violated usage blocks continuation")
        allowed = set(self.incident["original_started_ids"]) | set(self.incident["pending_trial_ids"])
        _require(set(trials) <= allowed and trials[FAILED_SLOT]["status"] == "failed", "historical trial state changed")
        for trial, state in trials.items():
            if trial == FAILED_SLOT or state["status"] == "completed":
                continue
            _require(trial == allow_active and state["status"] == "started", "new failure or unfinished slot blocks continuation")
        return [trial for trial in self.incident["pending_trial_ids"] if trial not in trials]

    def assert_execution_pin(self):
        super().assert_execution_pin()
        self._static()
        self._gate(self.events(), allow_active=self._active_trial)

    def start_trial(self, trial_id, *, metadata):
        self.assert_execution_pin()
        plan, _ = runner.verify_plan(self)
        matches = [s for s in plan["slots"] if s["trial_id"] == trial_id]
        _require(trial_id in self.incident["pending_trial_ids"] and len(matches) == 1
                 and all(metadata.get(k) == matches[0][k] for k in ("task_id", "split", "arm", "seed")),
                 "only the exact registered, previously unstarted slot may start")
        def check(trials, requests):
            self._static()
            self._gate(self._read())
            _require(trial_id not in trials, "never retry a started slot")
        event = self._append("trial_started", check=check, trial_id=trial_id, metadata=metadata)
        self._active_trial = trial_id
        return event

    def operational_status(self):
        try:
            self.assert_execution_pin()
            runner.verify_plan(self)
            remaining = self._gate(self.events())
            return {"ready": bool(remaining), "remaining_ids": remaining, "remaining": len(remaining),
                    "reason": None if remaining else "all_registered_slots_started"}
        except Exception:
            return {"ready": False, "remaining_ids": None, "remaining": None, "reason": "operational_guard_blocked"}


def safe_diagnostic(error, trial_directory, *, runtime_error_type=None):
    name = type(error).__name__ if error is not None else runtime_error_type
    name = name if isinstance(name, str) and _SAFE_NAME.fullmatch(name) else "UnavailableErrorType"
    result = {"error_type": name, "errno": None, "winerror": None, "filename_relative": None,
              "diagnostic_source": "caught_exception" if error is not None else "runtime_sanitized_type_only"}
    if error is None:
        return result
    for field in ("errno", "winerror"):
        value = getattr(error, field, None)
        if type(value) is int:
            result[field] = value
    filename = getattr(error, "filename", None)
    if isinstance(filename, str) and Path(filename).is_absolute():
        path = Path(filename)
        try:
            result["filename_relative"] = path.resolve().relative_to(Path(trial_directory).resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return result


def _event(campaign, phase, **fields):
    return campaign._append("operator_continuation", phase=phase, incident_id=INCIDENT_ID,
        incident_seal_sha256=campaign._seal_sha256, script_sha256=campaign.incident["script_sha256"],
        pending_ids_sha256=campaign.incident["pending_ids_sha256"], **fields)


async def run_continuation(campaign_root, incident_directory, output, *, max_new_trials=1, llm=None):
    _require(type(max_new_trials) is int and 1 <= max_new_trials <= 37, "explicit maximum must be 1..37")
    runner._assert_clean_model_environment()
    campaign = ContinuationCampaign(campaign_root, incident_directory)
    output = Path(output).resolve()
    _separate(output, campaign.root, campaign.incident_directory)
    with FileLock(str(campaign.incident_directory) + ".run.lock", timeout=0):
        gate = campaign.operational_status()
        _require(gate["ready"], "no safe unstarted slot is available")
        _require(gate["remaining"] != 37 or max_new_trials == 1, "first continuation must verify exactly one new slot")
        output.mkdir(parents=True, exist_ok=False)
        run_id, count, current = uuid4().hex, 0, None
        before_started = set(campaign.summary()["trials"])
        runner._exclusive_json(output / "run.json", {"incident_id": INCIDENT_ID, "run_id": run_id,
            "incident_seal_sha256": campaign._seal_sha256, "script_sha256": _script_hash(), "max_new_trials": max_new_trials})
        _event(campaign, "run_started", run_id=run_id, max_new_trials=max_new_trials)
        try:
            for trial in gate["remaining_ids"][:max_new_trials]:
                plan, tasks = runner.verify_plan(campaign)
                current = next(s for s in plan["slots"] if s["trial_id"] == trial)
                policy = _read(campaign.root / "research-policy.json")
                result = await runner.run_prepared_trial(campaign=campaign, trial_id=trial,
                    task=tasks[current["task_id"]], arm=current["arm"], seed=current["seed"],
                    policy_parameters=policy["parameters"] if current["arm"] == "C" else None, llm=llm)
                campaign._active_trial = None
                runtime_error = result["result"].report.get("error_type")
                if runtime_error is not None:
                    diagnostic = safe_diagnostic(None, campaign.root / "trials" / trial, runtime_error_type=runtime_error)
                    runner._exclusive_json(output / "runtime-diagnostic.json", diagnostic)
                    _event(campaign, "diagnostic", run_id=run_id, trial_id=trial, diagnostic=diagnostic)
                _require(campaign.operational_status()["ready"] or len(campaign._gate(campaign.events())) == 0,
                         "a new failure or unsettled request stopped continuation")
                count += 1
            _event(campaign, "run_finished", run_id=run_id, new_trials=count, outcome="batch_limit_or_complete")
            report = {"new_trials": count, "original_summary": campaign.summary(), "operational_gate": campaign.operational_status(),
                      "incident_id": INCIDENT_ID, "root_cause_fixed": False}
            runner._exclusive_json(output / "summary.json", report)
            return report
        except BaseException as error:
            campaign._active_trial = None
            started_count = len(set(campaign.summary()["trials"]) - before_started)
            directory = campaign.root / "trials" / current["trial_id"] if current else campaign.root / "trials"
            diagnostic = safe_diagnostic(error, directory)
            runner._exclusive_json(output / "controller-diagnostic.json", diagnostic)
            _event(campaign, "diagnostic", run_id=run_id, trial_id=current["trial_id"] if current else None, diagnostic=diagnostic)
            _event(campaign, "run_finished", run_id=run_id, new_trials=started_count, completed_new_trials=count, outcome="stopped")
            raise


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("register", "status", "run"))
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--incident", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--max-new-trials", type=int, default=1)
    args = parser.parse_args(argv)
    try:
        if args.command == "register":
            result = register(args.campaign, args.incident)
        elif args.command == "status":
            campaign = ContinuationCampaign(args.campaign, args.incident)
            result = {"original_summary": campaign.summary(), "operational_gate": campaign.operational_status()}
        else:
            if args.output is None:
                parser.error("--output is required for each new run")
            raw = runner.construct_live_llm()
            runner._scrub_cli_model_environment()
            result = asyncio.run(run_continuation(args.campaign, args.incident, args.output,
                                                 max_new_trials=args.max_new_trials, llm=raw))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as error:
        diagnostic = safe_diagnostic(error, args.output or args.incident)
        print(json.dumps({"status": "stopped", "diagnostic": diagnostic}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
