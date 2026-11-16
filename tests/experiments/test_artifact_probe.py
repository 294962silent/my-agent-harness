"""Mechanism evidence only: scripted fake decisions, no real provider requests."""

import json
import re
from pathlib import Path

import pytest

from experiments.benchmark_live import artifact_probe as probe
from experiments.benchmark_live.budget import BudgetLedger
from my_agent_llm import LLM, Response, Message


def fixtures(tmp_path):
    result = probe.freeze_fixtures(tmp_path / "fixtures")
    return Path(result["manifest"]), result["sha256"]


def client_factory(provider):
    def factory(config):
        llm = LLM(config)
        llm._provider = provider()
        return llm

    return factory


class ScriptedProvider:
    read_tool = "artifact_read"

    async def achat(self, messages, **kwargs):
        usage = {"prompt_tokens": 20, "completion_tokens": 5}

        def tool(name, args):
            return Response(
                content="",
                model="fake",
                usage=usage,
                tool_calls=[
                    {
                        "id": "call" + str(len(messages)),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                ],
            )

        question = next(m.text_content for m in messages if m.role == "user")
        key = re.search(r"key '([^']+)'", question).group(1)
        tools = [m for m in messages if m.role == "tool"]
        if not tools:
            return tool("fetch_log", {})
        latest = tools[-1].text_content
        value = re.search(re.escape(key) + r"=([0-9a-f]{32})", latest)
        if value:
            return Response(content=value.group(1), model="fake", usage=usage)
        handle = re.search(r"artifact:[0-9a-f]{16}:[0-9a-f]{32}", latest).group()
        return tool(
            self.read_tool,
            {"id": handle, "query": key}
            if self.read_tool == "artifact_search"
            else {"id": handle, "cursor": 0, "max_bytes": 16000},
        )


@pytest.mark.anyio
@pytest.mark.parametrize("read_tool", ["artifact_read", "artifact_search"])
async def test_real_component_roundtrip_schema_privacy_and_ephemeral_bodies(tmp_path, monkeypatch, read_tool):
    class Provider(ScriptedProvider):
        pass

    Provider.read_tool = read_tool
    manifest, fingerprint = fixtures(tmp_path)
    frozen = probe.load_fixtures(manifest, fingerprint)
    ledger = tmp_path / "ledger.json"
    BudgetLedger(ledger)
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_AUTH_SECRET")
    result = await probe.run_probe(
        fixtures=manifest,
        fixtures_sha256=fingerprint,
        ledger_path=ledger,
        spend_ceiling_usd=0.822059,
        output=tmp_path / "output",
        llm_factory=client_factory(Provider),
        workspace_parent=tmp_path / "scratch",
    )
    assert len(result["trials"]) == 4 and all(t["success"] for t in result["trials"])
    schemas = set()
    for trial in result["trials"]:
        events = [
            json.loads(line)
            for line in (tmp_path / "output" / trial["trial_id"] / "audit.jsonl").read_text().splitlines()
        ]
        requests = [e for e in events if e["event"] == "provider_entry"]
        assert all(e["boundary"] == "provider_method_entry_pre_sdk_not_wire" for e in requests)
        schemas.update(e["schema_sha256"] for e in requests)
        assert not requests[0]["target_visible"]
        if trial["arm"] == "on":
            assert any(
                e["stage"] == "after_fetch_before_read" and e["handle_present"] and not e["target_visible"]
                for e in requests
            )
            assert any(e["target_in_read_search_result"] for e in requests)
        assert trial["requests"] <= 4
    assert len(schemas) == 1
    exported = "\n".join(p.read_text() for p in (tmp_path / "output").rglob("*") if p.is_file())
    assert "SYNTHETIC_AUTH_SECRET" not in exported
    assert all(f.target not in exported and f.body not in exported for f in frozen)
    assert not list((tmp_path / "scratch").iterdir())
    assert BudgetLedger(ledger).snapshot()["max_requests"] == 16


def test_fixture_hidden_middle_and_hash_approval(tmp_path):
    manifest, fingerprint = fixtures(tmp_path)
    for fixture in probe.load_fixtures(manifest, fingerprint):
        assert 14000 <= len(fixture.body.encode()) <= 16000
        assert fixture.target not in fixture.body[:3500] + fixture.body[-1500:]
        assert fixture.body.count(fixture.target) == 1
    with pytest.raises(ValueError):
        probe.load_fixtures(manifest, "0" * 64)


def test_on_preflight_leak_is_rejected_before_reservation(tmp_path):
    manifest, fingerprint = fixtures(tmp_path)
    fixture = probe.load_fixtures(manifest, fingerprint)[0]
    audit = probe.MetadataAudit(tmp_path / "audit.jsonl", fixture, "on", "fixture-on", "run")
    llm = client_factory(ScriptedProvider)(probe.probe_config("offline"))
    ledger = BudgetLedger(tmp_path / "ledger.json", request_ceiling=4)
    guarded = probe.ProbeGuard(llm, ledger, "run", audit)
    with pytest.raises(probe.ProbeViolation):
        guarded._reserve([Message(role="user", content=fixture.target)], None, {})
    assert not ledger.snapshot()["requests"]
    assert fixture.target not in (tmp_path / "audit.jsonl").read_text()
    from my_agent_llm.observation import observation_context

    with observation_context(purpose="summary"):
        with pytest.raises(probe.ProbeViolation):
            guarded._reserve([Message(role="user", content="plain")], None, {})
    assert not ledger.snapshot()["requests"]


@pytest.mark.anyio
async def test_turn_and_locked_api_caps_and_phase_ceiling(tmp_path, monkeypatch):
    manifest, fingerprint = fixtures(tmp_path)
    ledger = tmp_path / "ledger.json"
    old = BudgetLedger(ledger)
    request = old.reserve("prior", input_bound=1900001, max_output=1715, purpose="agent")
    old.settle(request, usage={"prompt_tokens": 1900001, "completion_tokens": 1715}, status="ok")
    assert old.snapshot()["requests"][0]["cost_micro_usd"] == 572059
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "offline")

    class Repeating:
        calls = 0

        async def achat(self, *args, **kwargs):
            type(self).calls += 1
            assert kwargs["max_tokens"] == 1024
            return Response(
                content="",
                model="fake",
                usage={"prompt_tokens": 1, "completion_tokens": 1},
                tool_calls=[
                    {
                        "id": "again" + str(self.calls),
                        "type": "function",
                        "function": {"name": "fetch_log", "arguments": "{}"},
                    }
                ],
            )

    result = await probe.run_probe(
        fixtures=manifest,
        fixtures_sha256=fingerprint,
        ledger_path=ledger,
        spend_ceiling_usd=0.822059,
        output=tmp_path / "output",
        llm_factory=client_factory(Repeating),
    )
    assert Repeating.calls == 16 and all(t["requests"] == 4 for t in result["trials"])
    plan = json.loads((tmp_path / "output/plan.json").read_text())
    assert plan["phase_baseline_micro_usd"] == 572059 and plan["absolute_spend_ceiling_usd"] == "0.822059"
    assert old.snapshot()["max_requests"] == 16 and old.snapshot()["limit_micro_usd"] == 30_000_000
    with pytest.raises(ValueError):
        await probe.run_probe(
            fixtures=manifest,
            fixtures_sha256=fingerprint,
            ledger_path=ledger,
            spend_ceiling_usd=0.9,
            output=tmp_path / "forbidden",
            llm_factory=client_factory(Repeating),
        )
    assert not (tmp_path / "forbidden").exists()


@pytest.mark.anyio
async def test_guessing_target_without_fetch_and_read_does_not_pass_on(tmp_path, monkeypatch):
    manifest, fingerprint = fixtures(tmp_path)
    targets = {f.key: f.target for f in probe.load_fixtures(manifest, fingerprint)}
    ledger = tmp_path / "ledger.json"
    BudgetLedger(ledger)
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "offline")

    class Guesser:
        async def achat(self, messages, **kwargs):
            question = next(m.text_content for m in messages if m.role == "user")
            key = re.search(r"key '([^']+)'", question).group(1)
            return Response(content=targets[key], model="fake", usage={"prompt_tokens": 1, "completion_tokens": 1})

    result = await probe.run_probe(
        fixtures=manifest,
        fixtures_sha256=fingerprint,
        ledger_path=ledger,
        spend_ceiling_usd=0.822059,
        output=tmp_path / "output",
        llm_factory=client_factory(Guesser),
    )
    assert all(t["final_correct"] and not t["success"] for t in result["trials"])


@pytest.mark.anyio
async def test_unknown_usage_stops_entire_batch_without_retry(tmp_path, monkeypatch):
    manifest, fingerprint = fixtures(tmp_path)
    ledger = tmp_path / "ledger.json"
    BudgetLedger(ledger)
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "offline")

    class MissingUsage:
        calls = 0

        async def achat(self, *args, **kwargs):
            type(self).calls += 1
            return Response(content="done", model="fake")

    result = await probe.run_probe(
        fixtures=manifest,
        fixtures_sha256=fingerprint,
        ledger_path=ledger,
        spend_ceiling_usd=0.822059,
        output=tmp_path / "output",
        llm_factory=client_factory(MissingUsage),
    )
    assert MissingUsage.calls == 1 and len(result["trials"]) == 1
    assert result["halt_reason"] == "unknown_usage"
    row = BudgetLedger(ledger).snapshot()["requests"][0]
    assert row["cost_micro_usd"] is None and row["held_micro_usd"] > 0
