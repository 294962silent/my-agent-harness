"""Opt-in single paid request through the experiment observation owner.

No auth-file or dotenv fallback. Credentials come from environment, or the four
explicitly authorized LANGFUSE User/Machine variables with --windows-user-env.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

from experiments.benchmark_live.budget import create_live_llm
from experiments.benchmark_live.observation import ExperimentLLMOwner, close_owned_llm
from experiments.memory_validation.runner import TokenLedger, token_state
from my_agent_llm.models import Message
from my_coding_agent.settings import load_settings


LANGFUSE_NAMES = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_HOST", "LANGFUSE_BASE_URL")


def load_authorized_windows_environment():
    """Load only four authorized names into memory; return booleans only."""
    if os.name != "nt":
        raise ValueError("Windows registry loading requires Windows")
    import winreg
    presence = {}
    for label, hive, keypath in (("Machine", winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
            ("User", winreg.HKEY_CURRENT_USER, "Environment")):
        row = {}
        try:
            key = winreg.OpenKey(hive, keypath)
        except FileNotFoundError:
            key = None
        for name in LANGFUSE_NAMES:
            try:
                value = winreg.QueryValueEx(key, name)[0] if key else ""
            except FileNotFoundError:
                value = ""
            row[name] = bool(value)
            if value:
                os.environ[name] = value
        if key:
            winreg.CloseKey(key)
        presence[label] = row
    return presence


def write_receipt(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


async def run_probe(args):
    root = Path(args.output).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("probe output must be empty; existing requests are never retried")
    root.mkdir(parents=True, exist_ok=True)
    receipt = root / "receipt.json"
    state = {"created_at": datetime.now(timezone.utc).isoformat(), "status": "preflight",
             "campaign_id": args.campaign_id, "root_id": args.run_id,
             "trace_id": hashlib.sha256(args.run_id.encode()).hexdigest()[:32],
             "project_id": args.project_id, "metadata_only": True, "historical_backfill": False,
             "paid_request_cap": 1, "token_cap": 10000, "max_output_tokens": 128,
             "max_retries": 0, "credential_values_recorded": False}
    paths = {"owner": Path(__file__).with_name("observation.py"), "probe": Path(__file__),
             "budget": Path(__file__).with_name("budget.py")}
    state["source_sha256"] = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                              for name, path in paths.items()}
    owner, raw, ledger = None, None, None
    try:
        if args.windows_user_env:
            state["credential_presence"] = load_authorized_windows_environment()
        obs = load_settings().observation
        if not obs.enabled:
            raise ValueError("global observation settings must be enabled before a paid probe")
        if not os.environ.get(obs.public_key_env) or not os.environ.get(obs.secret_key_env):
            raise ValueError("Langfuse credentials unavailable")
        base = obs.base_url or os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
        if not base or not base.startswith("https://") or "/organization/" in base:
            raise ValueError("explicit HTTPS Langfuse base URL required")
        state["base_url"] = base.rstrip("/")
        state["session_id"] = args.campaign_id
        state["safe_metadata"] = {"campaign": args.campaign_id, "arm": "probe",
                                  "stage": "probe", "task_id": "live_validation"}
        write_receipt(receipt, state)
        raw = create_live_llm(max_output_tokens=128, timeout=120)
        owner = ExperimentLLMOwner.from_environment(raw, campaign_id=args.campaign_id, enabled=True,
            public_key_env=obs.public_key_env, secret_key_env=obs.secret_key_env, base_url=base,
            capacity=obs.capacity, network_timeout=obs.network_timeout, close_timeout=15)
        ledger = TokenLedger(root / "ledger.json", campaign_id=args.campaign_id,
                             max_requests=1, max_tokens=10000)
        ledger.register_run(args.run_id, max_requests=1, time_cap=120)
        state.update(status="observer_attached", model=raw.model, provider=raw.config.provider)
        write_receipt(receipt, state)
        response = await owner.achat(ledger, args.run_id,
            [Message(role="user", content="Reply with the single word READY.")],
            arm="probe", stage="probe", task_id="live_validation", max_tokens=128, temperature=0)
        state.update(status="model_settled", usage=response.usage, finish_reason=response.finish_reason,
                     outcome=response.outcome, content_present=bool(response.content.strip()))
    except BaseException as exc:
        state.update(status="stopped", error_type=type(exc).__name__)
    finally:
        if owner is not None:
            state["close"] = await owner.aclose()
        elif raw is not None:
            state["close"] = {"llm_clients_closed": await close_owned_llm(raw)}
        if ledger is not None:
            state["ledger"] = token_state(ledger.snapshot())
        if state["status"] == "model_settled":
            state["status"] = "settled_and_closed" if (
                state["close"].get("runtime_close_complete")
                and state["close"].get("llm_clients_closed")
                and not state["close"].get("export_errors")
            ) else "settled_export_error"
        state["remote_readback"] = "pending"
        if state.get("base_url"):
            state["trace_url"] = (state["base_url"] + "/project/" + args.project_id
                                  + "/traces/" + state["trace_id"])
        write_receipt(receipt, state)
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--single-paid-probe", action="store_true",
                        help="explicitly authorize exactly one new paid model request")
    parser.add_argument("--windows-user-env", action="store_true",
                        help="read only four LANGFUSE variables from Windows registry into process memory")
    parser.add_argument("--output", required=True, help="empty private directory outside immutable plans")
    parser.add_argument("--campaign-id", required=True, help="opaque stable Langfuse session identifier")
    parser.add_argument("--run-id", required=True, help="opaque durable root/trace identifier")
    parser.add_argument("--project-id", required=True, help="Langfuse project ID for the receipt link")
    args = parser.parse_args(argv)
    if not args.single_paid_probe:
        parser.error("--single-paid-probe is required; no provider call was made")
    try:
        state = asyncio.run(run_probe(args))
    except BaseException as exc:
        state = {"status": "stopped_before_dispatch", "error_type": type(exc).__name__}
    print(json.dumps(state))
    return 0 if state["status"] == "settled_and_closed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
