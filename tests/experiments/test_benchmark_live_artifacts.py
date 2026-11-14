"""Protocol v2: actual Agent loop -> handle -> production bounded artifact tools."""

import json
import re
from types import SimpleNamespace

import pytest

from experiments.benchmark_live import swe_agent as swe
from my_agent_llm import LLM, Response

BODY = "HEAD\n" + "a" * 7000 + "\nHIDDEN_MIDDLE_ERROR\n" + "b" * 7000 + "\nFINAL_ERROR_MARKER"


def call(name, args, identity):
    return {"id": identity, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


@pytest.mark.anyio
@pytest.mark.parametrize("kind", ["swe", "harbor"])
async def test_v2_projection_and_real_artifact_roundtrip_same_schema(tmp_path, monkeypatch, kind):
    if kind == "harbor":
        pytest.importorskip("harbor")
        from experiments.benchmark_live import harbor_agent as harbor
        from harbor.models.agent.context import AgentContext
        from harbor.models.task.task import Task
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "offline-key")
    schemas = []
    for arm in ("on", "off"):
        root = tmp_path / arm
        root.mkdir()
        state = {"calls": 0, "readback": False}

        class Provider:
            async def achat(self, messages, tools=None, **kwargs):
                state["calls"] += 1
                usage = {"prompt_tokens": 100, "completion_tokens": 20}

                def respond(calls=None):
                    return Response(content="" if calls else "done", model="offline", usage=usage, tool_calls=calls)

                if state["calls"] == 1:
                    schemas.append(tools)
                    return respond([call("shell", {"command": "visible-log"}, "shell1")])
                latest = next(m for m in reversed(messages) if m.role == "tool")
                text = latest.text_content
                if state["calls"] == 2:
                    if arm == "off":
                        assert "HIDDEN_MIDDLE_ERROR" in text and "FINAL_ERROR_MARKER" in text
                        assert "[ARTIFACT artifact:" not in text
                        return respond()
                    assert "HIDDEN_MIDDLE_ERROR" not in text
                    handle = re.search(r"artifact:[0-9a-f]{16}:[0-9a-f]{32}", text).group()
                    return respond([call("artifact_read", {"id": handle, "max_bytes": 48000}, "read1")])
                assert (latest.metadata or {}).get("tool_name") == "artifact_read"
                assert "HIDDEN_MIDDLE_ERROR" in text and "FINAL_ERROR_MARKER" in text
                assert "[end_of_artifact]" in text and "[ARTIFACT artifact:" not in text
                state["readback"] = True
                return respond()

        def factory(config):
            llm = LLM(config)
            llm._provider = Provider()
            return llm

        class Environment:
            async def start(self):
                pass

            async def exec(self, *args, **kwargs):
                if kind == "harbor":
                    return SimpleNamespace(return_code=0, stdout=BODY, stderr="")
                return {"exit_code": 0, "stdout": BODY, "stderr": ""}

            async def patch(self):
                return "diff --git a/a b/a\n"

            async def close(self):
                pass

        logs = root / "logs"
        if kind == "swe":
            metadata = root / "task.json"
            metadata.write_text(
                json.dumps(
                    {
                        "instance_id": swe.INSTANCE,
                        "problem_statement": "Inspect visible log",
                        "base_commit": swe.BASE,
                        "repo": "psf/requests",
                        "image": swe.IMAGE,
                    }
                )
            )
            official = root / "official.json"
            official.write_text(
                json.dumps(
                    {
                        "resolved_ids": [swe.INSTANCE],
                        "completed_instances": 1,
                        "resolved_instances": 1,
                        "error_instances": 0,
                    }
                )
            )
            gate = root / "gate.json"
            gate.write_text(
                json.dumps(
                    {
                        "instance_id": swe.INSTANCE,
                        **swe.PINNED[swe.INSTANCE],
                        "metadata_sha256": swe.sha(metadata),
                        "resolved": True,
                        "official_report_path": str(official),
                        "official_report_sha256": swe.sha(official),
                    }
                )
            )
            await swe.solve(
                metadata=metadata,
                gold_evidence=gate,
                ledger_path=tmp_path / "ledger.json",
                arm=arm,
                output=logs,
                environment=Environment(),
                llm_factory=factory,
            )
            report = json.loads((logs / "run.json").read_text())
        else:
            for directory in ("environment", "tests", "solution"):
                (root / directory).mkdir()
            (root / "instruction.md").write_text("Inspect visible log")
            (root / "task.toml").write_text('version="1.0"\n')
            (root / "environment/Dockerfile").write_text("FROM scratch\n")
            (root / "tests/test.sh").write_text("SECRET_VERIFIER")
            (root / "solution/solve.sh").write_text("SECRET_GOLD")
            gate = tmp_path / (arm + "-oracle.json")
            gate.write_text(
                json.dumps(
                    {
                        "task_name": root.name,
                        "agent_info": {"name": "oracle"},
                        "verifier_result": {"rewards": {"reward": 1}},
                        "task_checksum": Task(root).checksum,
                        "finished_at": "2026-11-12",
                    }
                )
            )
            monkeypatch.setattr(harbor, "LLM", factory)
            agent = harbor.BudgetedHarborAgent(
                task_dir=root,
                logs_dir=logs,
                oracle_result=gate,
                ledger_path=tmp_path / "ledger.json",
                artifact_projection=arm,
            )
            context = AgentContext()
            await agent.run("Inspect visible log", Environment(), context)
            report = context.metadata
        assert report["adapter_protocol"] == "v2"
        assert state["readback"] == (arm == "on")
        artifacts = list((logs / "artifacts").rglob("*.txt"))
        assert bool(artifacts) == (arm == "on")
        if artifacts:
            assert any("HIDDEN_MIDDLE_ERROR" in p.read_text() for p in artifacts)
        (logs / "v2_contract.json").write_text(
            json.dumps(
                {
                    "adapter_protocol": "v2",
                    "mode": "offline_fake_provider",
                    "provider_requests": 0,
                    "provider_usage_synthetic": True,
                    "adapter": kind,
                    "arm": arm,
                    "source_utf8_bytes": len(BODY.encode()),
                    "artifact_read_completed": state["readback"],
                    "full_source_in_view": arm == "off",
                    "hidden_middle_marker_absent_from_preview": arm == "on",
                    "artifact_files": len(artifacts),
                    "model_turns": state["calls"],
                    "tool_names": sorted(t["function"]["name"] for t in schemas[-1]),
                },
                indent=2,
            )
        )
    assert schemas[0] == schemas[1]
    assert {t["function"]["name"] for t in schemas[0]} == {"shell", "artifact_read", "artifact_search"}
