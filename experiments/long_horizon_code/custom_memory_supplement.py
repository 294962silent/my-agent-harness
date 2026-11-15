"""Serial sidecar for the three unstarted custom-memory v3 slots.

This driver never mutates plan.json, journal.json, or prior trial directories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from filelock import FileLock

from experiments.long_horizon_code import public_pilot as pilot


TASK = "custom-memory-heap-crash"
ARMS = ("loo_artifact", "baseline", "enhanced")
SLOT_INDICES = (6, 7, 8)
DEFAULT_OUTPUT = pilot.DEFAULT_OUTPUT


def sha_file(path: str | Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _custom_slots(plan: dict) -> list[dict]:
    selected = [slot for slot in plan["slots"] if slot["task"] == TASK]
    order = tuple(slot["arm"] for slot in selected)
    indices = tuple(plan["slots"].index(slot) for slot in selected)
    if order != ARMS or indices != SLOT_INDICES:
        raise pilot.PilotStop("frozen custom-memory slot order differs from the sidecar protocol")
    return selected


def _used_run_ids(ledger: dict) -> set[str]:
    used = {run_id for run_id, run in ledger["runs"].items() if run.get("trial_started")}
    used.update(row["run_id"] for row in ledger["requests"])
    return used


def _main_journal_has_custom(output: Path) -> list[str]:
    path = output / "journal.json"
    if not path.is_file():
        return []
    journal = pilot.read_json(path)
    return [run_id for run_id in journal if any(slot["run_id"] == run_id for slot in _custom_slots(pilot.read_json(output / "plan.json")))]


def _preflight_receipt(output: Path, expected_sha: str) -> dict:
    path = output / "readiness/custom-preflight.json"
    if not path.is_file():
        raise pilot.PilotStop("custom sidecar preflight receipt is missing")
    receipt = pilot.read_sealed(path)
    if (receipt.get("plan_sha256") != expected_sha or receipt.get("status") != "ready"
            or receipt.get("sidecar_sha256") != sha_file(__file__)):
        raise pilot.PilotStop("custom sidecar preflight is stale or not ready")
    return receipt


def preflight(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    expected_sha: str,
) -> dict:
    output, plan, book = pilot.validate_plan(output, expected_sha)
    with FileLock(str(output / "custom-driver.lock"), timeout=0):
        with FileLock(str(Path(plan["ledger"])) + ".driver.lock", timeout=0):
            pass
    slots = _custom_slots(plan)
    ledger_data = book.snapshot()
    used = _used_run_ids(ledger_data)
    journal_path = output / "journal-custom.json"
    custom_journal = pilot.read_json(journal_path) if journal_path.is_file() else {}
    preused = [
        slot["run_id"] for slot in slots
        if slot["run_id"] in used and slot["run_id"] not in custom_journal
    ]
    journal_custom = _main_journal_has_custom(output)
    blocked_custom_states = {
        run_id: state
        for run_id, state in custom_journal.items()
        if state.get("status") != "finished" or state.get("category") != "scored"
    }
    receipt = pilot._validate_oracle_receipt(output, plan, expected_sha)
    transport = pilot._validate_transport_receipt(output, plan, expected_sha)
    observation = pilot.langfuse_status(plan)
    credential_source = pilot.install_live_credential()
    proxy_ready = pilot.proxy_reachable(plan["runtime_proxy"])
    existing_preflight = pilot.read_sealed(output / "readiness/preflight.json")
    base_ready = existing_preflight.get("plan_sha256") == expected_sha and existing_preflight.get("status") == "ready"
    ready = all((
        not preused,
        not journal_custom,
        not blocked_custom_states,
        receipt.get("status") == "pass",
        transport.get("status") == "pass",
        observation["ready"],
        bool(credential_source),
        proxy_ready,
        base_ready,
    ))
    result = {
        "protocol": "lhpp-custom-memory-sidecar-v1",
        "status": "ready" if ready else "blocked",
        "plan_sha256": expected_sha,
        "sidecar_sha256": sha_file(__file__),
        "model_requests": 0,
        "task": TASK,
        "slots": [
            {"index": plan["slots"].index(slot), "arm": slot["arm"], "run_id": slot["run_id"]}
            for slot in slots
        ],
        "preused_run_ids": preused,
        "main_journal_custom_entries": journal_custom,
        "existing_custom_states": {
            slot["run_id"]: custom_journal[slot["run_id"]].get("status")
            for slot in slots if slot["run_id"] in custom_journal
        },
        "blocked_custom_states": blocked_custom_states,
        "oracle_ready": receipt.get("status") == "pass",
        "oracle_results": {
            task: row["result_path"] for task, row in receipt.get("tasks", {}).items()
        },
        "transport_ready": transport.get("status") == "pass",
        "langfuse": observation,
        "credential_source": credential_source,
        "proxy_reachable": proxy_ready,
        "base_preflight_ready": base_ready,
        "ledger": plan["ledger"],
        "journal_custom": str(output / "journal-custom.json"),
    }
    pilot.write_sealed(output / "readiness/custom-preflight.json", result)
    if not ready:
        raise pilot.PilotStop("custom sidecar preflight is blocked")
    return result


def _usage(ledger: pilot.BudgetLedger, run_id: str) -> dict:
    rows = [row for row in ledger.snapshot()["requests"] if row["run_id"] == run_id]
    complete = bool(rows) and all(row.get("status") == "ok" and row.get("cost_micro_usd") is not None for row in rows)
    return {
        "requests": len(rows),
        "usage_complete": complete,
        "input_tokens": sum(row["usage"]["prompt_tokens"] for row in rows) if complete else None,
        "output_tokens": sum(row["usage"]["completion_tokens"] for row in rows) if complete else None,
        "cost_usd": sum(row["cost_micro_usd"] for row in rows) / 1_000_000 if complete else None,
    }


def _adjudicate(directory: Path, slot: dict, book: pilot.BudgetLedger) -> dict | None:
    state = pilot.collect(directory, TASK, expected_task_name=slot["oracle_gate"]["task_name"])
    if state is None:
        return None
    state.update(_usage(book, slot["run_id"]))
    if state.get("status") != "finished" or state.get("category") != "scored":
        return state
    try:
        metadata = pilot._live_contract(directory, slot)
        state.update({
            "schema_sha256": metadata["schema_sha256"],
            "compaction_mode": metadata["compaction_mode"],
            "artifact_projection": metadata["artifact_projection"],
            "observation_mode": metadata["observation_mode"],
            "observation_required": metadata["observation_required"],
            "observation_ready": metadata["observation"]["ready"],
            "experiment_identity_sha256": metadata["experiment_identity_sha256"],
        })
    except pilot.PilotStop as exc:
        state["contract_error"] = str(exc)
    return state


def run(
    output: str | Path = DEFAULT_OUTPUT,
    *,
    expected_sha: str,
    execute: bool = False,
    max_new_trials: int = 1,
    executor=pilot.execute_harbor,
) -> dict:
    if type(max_new_trials) is not int or not 1 <= max_new_trials <= 3:
        raise pilot.PilotStop("max_new_trials must be 1..3")
    output = Path(output).resolve()
    receipt = preflight(output, expected_sha=expected_sha)
    _, plan, book = pilot.validate_plan(output, expected_sha)
    slots = _custom_slots(plan)
    if not execute:
        return {"preview": True, "model_requests": 0, "slots": slots}
    with FileLock(str(output / "custom-driver.lock"), timeout=0):
        with FileLock(str(Path(plan["ledger"])) + ".driver.lock", timeout=0):
            journal_path = output / "journal-custom.json"
            journal = pilot.read_json(journal_path) if journal_path.is_file() else {}
            new = 0
            for slot in slots:
                index = plan["slots"].index(slot)
                directory = output / f"custom-{index:02d}-{TASK}-{slot['arm']}"
                state = journal.get(slot["run_id"])
                if state:
                    if state.get("status") == "started":
                        journal[slot["run_id"]] = _adjudicate(directory, slot, book) or {
                            "status": "interrupted_unknown"
                        }
                        pilot.write_json(journal_path, journal, replace=True)
                    state = journal[slot["run_id"]]
                    if state.get("status") != "finished" or state.get("category") != "scored":
                        break
                    refreshed = _adjudicate(directory, slot, book)
                    if refreshed is not None:
                        journal[slot["run_id"]] = refreshed
                        pilot.write_json(journal_path, journal, replace=True)
                        state = refreshed
                    if state.get("contract_error") or state.get("usage_complete") is not True:
                        break
                    continue
                used = _used_run_ids(book.snapshot())
                if slot["run_id"] in used:
                    journal[slot["run_id"]] = {"status": "consumed_elsewhere"}
                    pilot.write_json(journal_path, journal, replace=True)
                    break
                if new >= max_new_trials:
                    break
                if directory.exists():
                    journal[slot["run_id"]] = {"status": "blocked_existing_directory"}
                    pilot.write_json(journal_path, journal, replace=True)
                    break
                pilot.validate_plan(output, expected_sha)
                fresh_oracle = receipt["oracle_results"][TASK]
                config = pilot.trial_config(plan, slot, directory, fresh_oracle)
                pilot._trial_config_validate(config)
                directory.mkdir(parents=True)
                pilot.write_json(directory / "job.json", config)
                journal[slot["run_id"]] = {"status": "started"}
                pilot.write_json(journal_path, journal, replace=True)
                with book.transaction() as data:
                    run_row = data["runs"].get(slot["run_id"])
                    if run_row is None or run_row.get("trial_started"):
                        raise pilot.PilotStop("frozen sidecar run is missing or already consumed")
                    run_row["trial_started"] = True
                new += 1
                try:
                    executor(config, directory, slot["limits"]["max_seconds"] + 3600)
                except BaseException as exc:
                    journal[slot["run_id"]] = _adjudicate(directory, slot, book) or {
                        "status": "infrastructure_error",
                        "error_type": type(exc).__name__,
                    }
                else:
                    journal[slot["run_id"]] = _adjudicate(directory, slot, book) or {
                        "status": "missing_official_result"
                    }
                pilot.write_json(journal_path, journal, replace=True)
                state = journal[slot["run_id"]]
                if (state.get("status") != "finished" or state.get("category") != "scored"
                        or state.get("suspected_verifier_infra") or state.get("usage_complete") is not True
                        or state.get("contract_error")):
                    break
                completed = [
                    journal[item["run_id"]] for item in slots
                    if journal.get(item["run_id"], {}).get("status") == "finished"
                    and journal[item["run_id"]].get("category") == "scored"
                ]
                schemas = {row.get("schema_sha256") for row in completed}
                if len(completed) >= 2 and (None in schemas or len(schemas) != 1):
                    journal[slot["run_id"]]["contract_error"] = "schema differs within custom-memory triplet"
                    pilot.write_json(journal_path, journal, replace=True)
                    break
            return summary(output)


def summary(output: str | Path = DEFAULT_OUTPUT) -> dict:
    output = Path(output).resolve()
    plan = pilot.read_json(output / "plan.json")
    slots = _custom_slots(plan)
    journal = pilot.read_json(output / "journal-custom.json") if (output / "journal-custom.json").is_file() else {}
    rows = []
    for slot in slots:
        state = journal.get(slot["run_id"], {"status": "not_started"})
        usage = _usage(pilot.BudgetLedger(plan["ledger"]), slot["run_id"])
        rows.append({"task": TASK, "arm": slot["arm"], "run_id": slot["run_id"], **state, **usage})
    complete = all(row.get("status") == "finished" and row.get("category") == "scored" for row in rows)
    schemas = {row.get("schema_sha256") for row in rows}
    observation_ready = all(row.get("observation_ready") is True for row in rows)
    result = {
        "protocol": "lhpp-custom-memory-sidecar-v1",
        "task": TASK,
        "rows": rows,
        "complete_scored_triplet": complete,
        "schema_consistent": complete and len(schemas) == 1 and None not in schemas,
        "observation_ready": observation_ready,
        "model_requests": sum(row["requests"] for row in rows),
    }
    if complete and result["schema_consistent"] and observation_ready:
        rewards = {row["arm"]: row["official_reward"] for row in rows}
        result["rewards"] = rewards
        result["enhanced_minus_baseline"] = rewards["enhanced"] - rewards["baseline"]
        result["enhanced_minus_loo"] = rewards["enhanced"] - rewards["loo_artifact"]
    pilot.write_json(output / "summary-custom.json", result, replace=True)
    return result


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("preflight", "run", "summary"):
        command = sub.add_parser(name)
        command.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
        if name != "summary":
            command.add_argument("--expected-sha", required=True)
        if name == "run":
            command.add_argument("--execute", action="store_true")
            command.add_argument("--max-new-trials", type=int, default=1)
    args = vars(parser.parse_args(argv))
    command = args.pop("command")
    result = {"preflight": preflight, "run": run, "summary": summary}[command](**args)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
