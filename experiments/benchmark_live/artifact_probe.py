"""Isolated artifact capability probe; metadata audit is pre-SDK, never wire evidence."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import random
import re
import secrets
import tempfile
from uuid import uuid4

from my_agent_core.agent import Agent
from my_agent_core.artifacts import ArtifactStore
from my_agent_core.events import AgentEnd
from my_agent_core.retry import AutoRetryPolicy
from my_agent_core.run_lifecycle import RunLimits
from my_agent_core.session import Session
from my_agent_core.tools import tool
from my_agent_llm import Config, LLM
from my_agent_llm.observation import _CONTEXT
from my_coding_agent.tools.artifact import make_artifact_tools

from .budget import BudgetLedger, BudgetStop, GuardedLLM

PROTOCOL = "artifact-capability-probe-v1"
BOUNDARY = "provider_method_entry_pre_sdk_not_wire"
MAX_ABSOLUTE_MICRO_USD = 822059
PHASE_MICRO_USD = 250000
TOOLS = frozenset(("fetch_log", "artifact_read", "artifact_search"))
HANDLE = re.compile(r"artifact:[0-9a-f]{16}:[0-9a-f]{32}")


def sha_bytes(value):
    return hashlib.sha256(value).hexdigest()


def _write(path, value):
    Path(path).write_text(json.dumps(value, indent=2), encoding="utf-8")


@dataclass(frozen=True)
class Fixture:
    identity: str
    key: str
    sha256: str
    body: str = field(repr=False)
    target: str = field(repr=False)


def freeze_fixtures(output):
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    rows = []
    for suffix in ("a", "b"):
        key = "diagnostic_code_" + suffix
        target = secrets.token_hex(16)
        body = (
            "INFO public_padding="
            + ("a" * 7000)
            + "\n"
            + key
            + "="
            + target
            + "\nINFO trailing_padding="
            + ("b" * 7000)
            + "\nEND\n"
        )
        name = "fixture-" + suffix + ".log"
        raw = body.encode()
        (root / name).write_bytes(raw)
        rows.append(
            {
                "id": "fixture-" + suffix,
                "key": key,
                "file": name,
                "sha256": sha_bytes(raw),
                "target_sha256": sha_bytes(target.encode()),
            }
        )
    manifest = root / "fixtures.json"
    _write(manifest, {"schema_version": 1, "fixtures": rows})
    return {"manifest": str(manifest), "sha256": sha_bytes(manifest.read_bytes()), "provider_requests": 0}


def load_fixtures(manifest, expected_sha256):
    path = Path(manifest).resolve()
    raw = path.read_bytes()
    if sha_bytes(raw) != expected_sha256:
        raise ValueError("fixture manifest checksum differs from approval")
    document = json.loads(raw)
    if (
        set(document) != {"schema_version", "fixtures"}
        or type(document["schema_version"]) is not int
        or document["schema_version"] != 1
        or len(document["fixtures"]) != 2
    ):
        raise ValueError("probe requires exactly two frozen fixtures")
    result = []
    seen = set()
    for row in document["fixtures"]:
        if set(row) != {"id", "key", "file", "sha256", "target_sha256"}:
            raise ValueError("invalid fixture fields")
        if not re.fullmatch(r"[a-z0-9_-]{1,40}", row["id"]) or row["id"] in seen:
            raise ValueError("invalid fixture ID")
        if not re.fullmatch(r"[a-zA-Z_][a-zA-Z_0-9]{0,63}", row["key"]):
            raise ValueError("invalid public fixture key")
        source = (path.parent / row["file"]).resolve()
        if not source.is_relative_to(path.parent) or Path(row["file"]).is_absolute():
            raise ValueError("fixture path escaped approved root")
        body_bytes = source.read_bytes()
        if sha_bytes(body_bytes) != row["sha256"] or not 14000 <= len(body_bytes) <= 16000:
            raise ValueError("fixture size/hash mismatch")
        body = body_bytes.decode("utf-8")
        values = re.findall(r"(?m)^" + re.escape(row["key"]) + r"=([0-9a-f]{32})$", body)
        if len(values) != 1:
            raise ValueError("fixture must contain one nonce for the public key")
        target = values[0]
        if (
            sha_bytes(target.encode()) != row["target_sha256"]
            or body.count(target) != 1
            or target in body[:3500] + body[-1500:]
        ):
            raise ValueError("target is not uniquely hidden in the fixture middle")
        result.append(Fixture(row["id"], row["key"], row["sha256"], body, target))
        seen.add(row["id"])
    return result


class ProbeViolation(BudgetStop):
    pass


class MetadataAudit:
    """Only fixed metadata fields leave this object; body/target are never logged."""

    def __init__(self, path, fixture, arm, trial_id, run_id):
        self.path = Path(path)
        self.fixture = fixture
        self.arm = arm
        self.trial_id = trial_id
        self.run_id = run_id
        self.entries = []

    def append(self, value):
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(value) + "\n")

    def inspect(self, messages, tools):
        encoded = json.dumps(
            {"messages": [m.model_dump() for m in messages], "tools": tools}, ensure_ascii=False, default=str
        )
        tool_messages = [m for m in messages if m.role == "tool"]
        fetched = any((m.metadata or {}).get("tool_name") == "fetch_log" for m in tool_messages)
        read = [m for m in tool_messages if (m.metadata or {}).get("tool_name") in ("artifact_read", "artifact_search")]
        return {
            "trial_id": self.trial_id,
            "run_id": self.run_id,
            "schema_sha256": sha_bytes(json.dumps(tools, sort_keys=True, separators=(",", ":")).encode()),
            "tool_names": sorted(
                t["function"]["name"] if t.get("function", {}).get("name") in TOOLS else "unrecognized_tool"
                for t in (tools or [])
            ),
            "stage": "after_read_or_search" if read else "after_fetch_before_read" if fetched else "before_fetch",
            "handle_present": bool(HANDLE.search(encoded)),
            "target_visible": self.fixture.target in encoded,
            "target_in_read_search_result": any(self.fixture.target in (m.text_content or "") for m in read),
            "fetch_log_response_present": fetched,
            "read_search_tool_names": sorted({(m.metadata or {})["tool_name"] for m in read}),
        }

    def preflight(self, messages, tools):
        facts = self.inspect(messages, tools)
        leaked = facts["target_visible"] and (
            not facts["fetch_log_response_present"] or (self.arm == "on" and not facts["target_in_read_search_result"])
        )
        if leaked:
            self.append({"event": "preflight_rejection", "boundary": "guard_pre_reservation_not_provider", **facts})
            raise ProbeViolation("probe target would leak before permitted tool evidence")
        return facts


class AuditedProvider:
    def __init__(self, provider, audit):
        self.provider = provider
        self.audit = audit

    def __getattr__(self, name):
        return getattr(self.provider, name)

    async def achat(self, messages, *, tools=None, **kwargs):
        facts = self.audit.preflight(messages, tools)
        record = {
            "event": "provider_entry",
            "boundary": BOUNDARY,
            "method": "achat",
            "call_index": len(self.audit.entries) + 1,
            **facts,
        }
        self.audit.entries.append(record)
        self.audit.append(record)
        try:
            response = await self.provider.achat(messages, tools=tools, **kwargs)
        except BaseException as exc:
            self.audit.append(
                {
                    "event": "provider_failure",
                    "boundary": BOUNDARY,
                    "call_index": record["call_index"],
                    "error_type": type(exc).__name__,
                }
            )
            raise
        names = []
        for call in response.tool_calls or []:
            name = call.get("name") or (call.get("function") or {}).get("name") if isinstance(call, dict) else call.name
            names.append(name if name in TOOLS else "unrecognized_tool")
        self.audit.append(
            {
                "event": "provider_return",
                "boundary": BOUNDARY,
                "call_index": record["call_index"],
                "tool_names": names,
                "target_in_response": self.audit.fixture.target in (response.content or ""),
            }
        )
        return response

    def chat(self, *args, **kwargs):
        raise ProbeViolation("probe only permits audited async nonstreaming provider calls")

    def stream(self, *args, **kwargs):
        raise ProbeViolation("probe only permits audited async nonstreaming provider calls")

    async def achat_stream(self, *args, **kwargs):
        raise ProbeViolation("probe only permits audited async nonstreaming provider calls")

    async def astream_events(self, *args, **kwargs):
        raise ProbeViolation("probe only permits audited async nonstreaming provider calls")


class ProbeGuard(GuardedLLM):
    def __init__(self, llm, ledger, run_id, audit):
        super().__init__(llm, ledger, run_id)
        self.audit = audit

    def _reserve(self, messages, tools, kwargs):
        if _CONTEXT.get().purpose not in (None, "agent"):
            raise ProbeViolation("auxiliary model calls are disabled in the probe")
        self.audit.preflight(messages, tools)
        kwargs["max_tokens"] = min(int(kwargs.get("max_tokens") or 1024), 1024)
        return super()._reserve(messages, tools, kwargs)


def probe_config(key):
    return Config(
        provider="openai",
        model="deepseek-v4.1-flash",
        api_key=key,
        base_url="https://opencode.ai/inference/go/openai/v1",
        temperature=0.7,
        max_tokens=1024,
        timeout=60,
        max_retries=0,
    )


async def _trial(fixture, arm, root, ledger, llm_factory, workspace_parent):
    identity = fixture.identity + "-" + arm
    output = root / identity
    output.mkdir()
    run_id = uuid4().hex
    audit = MetadataAudit(output / "audit.jsonl", fixture, arm, identity, run_id)
    _write(output / "state.json", {"status": "started", "run_id": run_id})
    raw = llm_factory(probe_config(os.environ["LIVE_BENCH_API_KEY"]))
    raw._provider = AuditedProvider(raw._provider, audit)
    guarded = ProbeGuard(raw, ledger, run_id, audit)
    terminal = {}
    final = None
    error = None
    scratch_parent = Path(workspace_parent or tempfile.gettempdir()).resolve()
    scratch_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="artifact-probe-", dir=scratch_parent) as scratch_name:
        scratch = Path(scratch_name).resolve()
        if scratch.parent != scratch_parent:
            raise ValueError("temporary probe workspace escaped intended parent")

        @tool(name="fetch_log", description="Fetch the controlled log for this question.", is_parallel_safe=True)
        def fetch_log():
            return fixture.body

        store = ArtifactStore(scratch)
        agent = Agent(
            llm=guarded,
            tools=[fetch_log, *make_artifact_tools(store)],
            session=Session(path=scratch / "session.jsonl"),
            system_prompt="Answer the user's log question using the available tools. Return only the requested value.",
            context_budget=1_000_000,
            artifact_store=store if arm == "on" else None,
            memory_dir=False,
            task_store=False,
            skill_dirs=[],
            subagent_dirs=[],
            extension_dirs=[],
            plugin_dirs=[],
            retry_policy=AutoRetryPolicy(max_retries=0),
            run_limits=RunLimits(max_turns=4, max_seconds=60),
        )

        def record(event):
            if isinstance(event, AgentEnd):
                terminal.update(stop_reason=event.stop_reason)

        agent.subscribe(record)
        try:
            final = await asyncio.wait_for(
                agent.run("What is the value for key '" + fixture.key + "' in the log? Return only its value."),
                timeout=60,
            )
        except asyncio.CancelledError:
            error = "CancelledError"
            raise
        except Exception as exc:
            error = type(exc).__name__
        finally:
            await agent.aclose()
    # Sessions/artifact bodies existed only in the deleted, owned temporary workspace.
    requests = [r for r in ledger.snapshot()["requests"] if r["run_id"] == run_id]
    usage_known = bool(requests) and all(r["cost_micro_usd"] is not None for r in requests)
    correct = isinstance(final, str) and final.strip() == fixture.target
    hidden_steps = [
        i
        for i, e in enumerate(audit.entries)
        if e["stage"] == "after_fetch_before_read" and e["handle_present"] and not e["target_visible"]
    ]
    hidden = bool(hidden_steps)
    revealed = any(e["target_in_read_search_result"] for e in audit.entries)
    ordered_read = any(
        h < i for h in hidden_steps for i, e in enumerate(audit.entries) if e["target_in_read_search_result"]
    )
    premature = any(
        e["target_visible"]
        and not e["fetch_log_response_present"]
        or (arm == "on" and e["target_visible"] and not e["target_in_read_search_result"])
        for e in audit.entries
    )
    successful = (
        error is None
        and correct
        and not premature
        and (
            (hidden and revealed and ordered_read)
            if arm == "on"
            else any(e["stage"] != "before_fetch" and e["target_visible"] for e in audit.entries)
        )
    )
    result = {
        "trial_id": identity,
        "fixture_id": fixture.identity,
        "fixture_sha256": fixture.sha256,
        "arm": arm,
        "run_id": run_id,
        "status": error or terminal.get("stop_reason", "missing_terminal_event"),
        "requests": len(requests),
        "usage_available": usage_known,
        "cost_usd": sum(r["cost_micro_usd"] for r in requests) / 1_000_000 if usage_known else None,
        "success": successful,
        "final_correct": correct,
        "fetch_preview_hidden_with_handle": hidden,
        "read_search_result_revealed_target": revealed,
        "hidden_fetch_preceded_read": ordered_read,
        "premature_target_exposure": premature,
        "schema_hashes": sorted({e["schema_sha256"] for e in audit.entries}),
        "audit_boundary": BOUNDARY,
        "temporary_bodies_deleted": True,
    }
    _write(output / "result.json", result)
    _write(output / "state.json", {"status": "finished", "run_id": run_id})
    return result


async def run_probe(
    *, fixtures, fixtures_sha256, ledger_path, spend_ceiling_usd, output, llm_factory=LLM, workspace_parent=None
):
    frozen = load_fixtures(fixtures, fixtures_sha256)
    requested = Decimal(str(spend_ceiling_usd))
    if not requested.is_finite() or not 0 < requested <= Decimal(MAX_ABSOLUTE_MICRO_USD) / 1_000_000:
        raise ValueError("probe absolute spend ceiling must be positive and at most 0.822059 USD")
    ledger_path = Path(ledger_path).resolve()
    if not ledger_path.is_file():
        raise ValueError("reuse the existing global ledger")
    ledger = BudgetLedger(ledger_path, request_ceiling=4)
    before = ledger.snapshot()
    if any(r["cost_micro_usd"] is None for r in before["requests"]):
        raise ValueError("global ledger contains unknown usage")
    baseline = sum(r["cost_micro_usd"] for r in before["requests"])
    ceiling = min(int(requested * 1_000_000), MAX_ABSOLUTE_MICRO_USD, baseline + PHASE_MICRO_USD)
    if baseline >= ceiling:
        raise BudgetStop("probe absolute phase budget already exhausted")
    ledger = BudgetLedger(ledger_path, request_ceiling=4, spend_ceiling_usd=ceiling / 1_000_000)
    if not os.environ.get("LIVE_BENCH_API_KEY"):
        raise ValueError("LIVE_BENCH_API_KEY is required only in the host environment")
    root = Path(output).resolve()
    root.mkdir(parents=True, exist_ok=False)
    order = list(frozen)
    random.Random(23).shuffle(order)
    planned = [
        {"fixture_id": f.identity, "arm": arm}
        for i, f in enumerate(order)
        for arm in (("on", "off") if i == 0 else ("off", "on"))
    ]
    _write(
        root / "plan.json",
        {
            "protocol": PROTOCOL,
            "fixtures_sha256": fixtures_sha256,
            "seed": 23,
            "trials": planned,
            "max_requests_per_run": 4,
            "max_turns": 4,
            "max_output_tokens": 1024,
            "max_seconds": 60,
            "root_policy_max_requests": 16,
            "phase_baseline_micro_usd": baseline,
            "absolute_spend_ceiling_usd": str(Decimal(ceiling) / 1_000_000),
            "audit_boundary": BOUNDARY,
            "wire_capture": False,
        },
    )
    result = {
        "protocol": PROTOCOL,
        "evidence_level": "isolated_capability_probe_not_SWE_quality",
        "trials": [],
        "halt_reason": None,
    }
    try:
        for trial in planned:
            f = next(f for f in frozen if f.identity == trial["fixture_id"])
            item = await _trial(f, trial["arm"], root, ledger, llm_factory, workspace_parent)
            result["trials"].append(item)
            snapshot = ledger.snapshot()
            if any(r["cost_micro_usd"] is None for r in snapshot["requests"]):
                result["halt_reason"] = "unknown_usage"
                break
            if item["status"] not in ("end_turn", "budget_exhausted"):
                result["halt_reason"] = "trial_error"
                break
    except asyncio.CancelledError:
        result["halt_reason"] = "cancelled"
        raise
    finally:
        hashes = {h for r in result["trials"] for h in r["schema_hashes"]}
        result["schema_hashes_equal"] = (
            (len(hashes) == 1) if len(result["trials"]) == 4 else False if len(hashes) > 1 else None
        )
        _write(root / "summary.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("freeze-fixtures").add_argument("--output", required=True)
    run = sub.add_parser("run")
    for name in ("fixtures", "fixtures-sha256", "ledger-path", "spend-ceiling-usd", "output"):
        run.add_argument("--" + name, required=True)
    options = vars(parser.parse_args(argv))
    action = options.pop("action")
    try:
        result = freeze_fixtures(**options) if action == "freeze-fixtures" else asyncio.run(run_probe(**options))
    except Exception as exc:
        print(json.dumps({"status": "stopped", "error_type": type(exc).__name__}))
        return 1
    print(json.dumps(result, indent=2))
    return 1 if result.get("halt_reason") else 0


if __name__ == "__main__":
    raise SystemExit(main())
