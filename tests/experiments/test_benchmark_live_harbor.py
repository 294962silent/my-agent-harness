"""Real Harbor API / fake provider and container: zero network, no benchmark claim."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")

from experiments.benchmark_live import harbor_agent as adapter
from harbor.models.agent.context import AgentContext
from harbor.models.task.task import Task
from my_agent_llm import LLM, Response


@pytest.mark.anyio
async def test_adapter_runs_guarded_loop_without_host_test_leakage(tmp_path, monkeypatch):
    task = tmp_path / "official-fixture"
    for directory in ("environment", "tests", "solution"):
        (task / directory).mkdir(parents=True)
    (task / "instruction.md").write_text("Inspect visible workspace")
    (task / "task.toml").write_text('version = "1.0"\n')
    (task / "environment/Dockerfile").write_text("FROM scratch\n")
    (task / "tests/test.sh").write_text("SECRET_VERIFIER")
    (task / "solution/solve.sh").write_text("SECRET_GOLD")
    oracle = tmp_path / "oracle.json"
    oracle.write_text(
        json.dumps(
            {
                "task_name": task.name,
                "agent_info": {"name": "oracle"},
                "verifier_result": {"rewards": {"reward": 1}},
                "task_checksum": Task(task).checksum,
                "finished_at": "2026-11-12",
            }
        )
    )
    received, commands = [], []

    class Provider:
        calls = 0

        async def achat(self, messages, **kwargs):
            received.extend(m.text_content or "" for m in messages)
            self.calls += 1
            usage = {"prompt_tokens": 100, "completion_tokens": 20}
            if self.calls == 1:
                return Response(
                    model="fake",
                    content="",
                    usage=usage,
                    tool_calls=[
                        {
                            "id": "shell1",
                            "type": "function",
                            "function": {"name": "shell", "arguments": '{"command":"pwd"}'},
                        }
                    ],
                )
            return Response(model="fake", content="done", usage=usage)

    provider = Provider()

    def client(config):
        llm = LLM(config)
        llm._provider = provider
        return llm

    monkeypatch.setattr(adapter, "LLM", client)
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_KEY")

    class Environment:
        async def exec(self, command, timeout_sec):
            commands.append(command)
            return SimpleNamespace(return_code=0, stdout="/workspace", stderr="")

    agent = adapter.BudgetedHarborAgent(
        task_dir=task, logs_dir=tmp_path / "logs", oracle_result=oracle, ledger_path=tmp_path / "ledger.json"
    )
    context = AgentContext()
    await agent.run("Inspect visible workspace", Environment(), context)
    assert commands == ["pwd"] and provider.calls == 2
    assert context.metadata["status"] == "end_turn"
    assert context.n_input_tokens == 200 and context.n_output_tokens == 40
    assert not any(s in "\n".join(received) for s in ("SECRET_GOLD", "SECRET_VERIFIER", "SYNTHETIC_KEY"))
    assert "SYNTHETIC_KEY" not in (tmp_path / "logs/live_run.json").read_text()
