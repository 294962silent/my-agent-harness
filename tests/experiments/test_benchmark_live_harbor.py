"""Real Harbor API / fake provider and container: zero network, no benchmark claim."""

import json
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")

from experiments.benchmark_live import harbor_agent as adapter
from experiments.long_horizon_quality.orchestrator import RoleRequest
from harbor.models.agent.context import AgentContext
from harbor.models.task.task import Task
from my_agent_llm import Config, LLM, Message, Response, TurnOutcome


def test_auditor_command_allowlist_accepts_read_only_git_and_rejects_mutation():
    for command in (
        "git branch -a",
        "git reflog",
        "git show HEAD",
        "git rev-parse HEAD",
        "git cat-file -p HEAD",
        "python -m pytest -q",
    ):
        allowed, _detail = adapter._safe_auditor_command(command)
        assert allowed is True
    for command in (
        "git branch -D main",
        "git reflog expire --all",
        "git reset --hard HEAD",
        "python script.py",
        "find . -delete",
        "ls | head",
    ):
        allowed, _detail = adapter._safe_auditor_command(command)
        assert allowed is False


def test_executor_budget_shrinks_reserve_for_terminal_round():
    assert adapter._executor_turn_budget(
        available=63,
        round_index=1,
        max_rounds=3,
    ) == 16
    assert adapter._executor_turn_budget(
        available=42,
        round_index=2,
        max_rounds=3,
    ) == 32
    assert adapter._executor_turn_budget(
        available=15,
        round_index=3,
        max_rounds=3,
    ) == 9
    with pytest.raises(ValueError, match="outside"):
        adapter._executor_turn_budget(
            available=15,
            round_index=4,
            max_rounds=3,
        )


@pytest.mark.anyio
async def test_manager_repairs_one_invalid_json_response(tmp_path):
    valid = json.dumps(
        {
            "next": "done",
            "reason": "verified",
            "subtask": "",
            "acceptance_criteria": [],
            "question": "",
            "related_rounds": [1],
        }
    )

    class Provider:
        def __init__(self):
            self.calls = []
            self.responses = [
                Response(
                    model="fake",
                    content="",
                    usage={"prompt_tokens": 10, "completion_tokens": 4096},
                    outcome=TurnOutcome.LENGTH,
                    finish_reason="length",
                ),
                Response(
                    model="fake",
                    content=valid,
                    usage={"prompt_tokens": 10, "completion_tokens": 20},
                    outcome=TurnOutcome.COMPLETED,
                    finish_reason="stop",
                ),
            ]

        async def achat(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return self.responses.pop(0)

    provider = Provider()
    request = RoleRequest(
        purpose="mea_manager_r2",
        messages=(
            Message(role="system", content="manager system"),
            Message(role="user", content="current state"),
        ),
    )
    role_log = tmp_path / "quality_roles.jsonl"

    output = await adapter._request_manager_json(
        llm=provider,
        request=request,
        remaining_timeout=lambda: 30,
        role_log=role_log,
    )

    assert output == valid
    assert len(provider.calls) == 2
    assert all(
        kwargs["response_format"] == {"type": "json_object"}
        for _messages, kwargs in provider.calls
    )
    assert "Correction:" in provider.calls[1][0][-1].content
    rows = [
        json.loads(line)
        for line in role_log.read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert [row["attempt"] for row in rows] == [1, 2]
    assert rows[0]["outcome"] == "length"
    assert rows[1]["outcome"] == "completed"


def test_audit_transcript_keeps_latest_tool_evidence_and_is_bounded():
    rendered = adapter._render_audit_transcript(
        [
            Message(role="user", content="old-context " * 3_000),
            Message(
                role="assistant",
                content="",
                metadata={
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "audit_shell",
                                "arguments": '{"command":"git status"}',
                            },
                        }
                    ]
                },
            ),
            Message(
                role="tool",
                content="working tree clean",
                metadata={"tool_call_id": "call-1"},
            ),
        ],
        limit=600,
    )

    assert len(rendered) <= 600
    assert "audit_shell" in rendered
    assert "tool_call_id=call-1" in rendered
    assert "working tree clean" in rendered
    assert "old-context" not in rendered


def test_fallback_auditor_report_is_parseable_and_fail_closed():
    report = adapter._fallback_audit_report(
        validation_error="invalid JSON output"
    )
    parsed = adapter.parse_audit_report(report)

    assert parsed.status == "blocked"
    assert parsed.verified_facts == ()
    assert parsed.remaining


@pytest.mark.anyio
async def test_auditor_projection_uses_transcript_in_json_mode(tmp_path):
    projected = json.dumps(
        {
            "status": "complete",
            "summary": "verified from transcript",
            "evidence": ["git status returned clean"],
            "verified_facts": ["working tree clean"],
            "remaining": [],
        }
    )

    class Provider:
        def __init__(self):
            self.calls = []

        async def achat(self, messages, **kwargs):
            self.calls.append((messages, kwargs))
            return Response(
                model="fake",
                content=projected,
                usage={"prompt_tokens": 100, "completion_tokens": 40},
                outcome=TurnOutcome.COMPLETED,
                finish_reason="stop",
            )

    provider = Provider()
    request = RoleRequest(
        purpose="mea_auditor_r2",
        messages=(
            Message(role="system", content="auditor system"),
            Message(role="user", content="original audit request"),
        ),
    )
    role_log = tmp_path / "quality_roles.jsonl"

    output = await adapter._request_auditor_projection(
        llm=provider,
        request=request,
        transcript="[tool] tool_call_id=call-1\nworking tree clean",
        last_output="The residual line is unchanged.",
        validation_error="invalid JSON",
        remaining_timeout=lambda: 30,
        role_log=role_log,
    )

    assert output == projected
    assert len(provider.calls) == 1
    messages, kwargs = provider.calls[0]
    assert kwargs["tools"] == []
    assert kwargs["response_format"] == {"type": "json_object"}
    assert "working tree clean" in messages[-1].content
    assert "Return exactly one JSON object" in messages[-1].content
    row = json.loads(role_log.read_text(encoding="utf-8").splitlines()[-1])
    assert row["mode"] == "transcript_json_projection"
    assert row["attempt"] == 3


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

    monkeypatch.setattr(adapter, "create_live_llm", lambda **kw: client(Config(
        provider="openai", model="deepseek-v4.1-flash", api_key="SYNTHETIC_KEY", max_retries=0,
        max_tokens=kw["max_output_tokens"],
    )))
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
    assert context.n_cache_tokens is None
    assert not any(s in "\n".join(received) for s in ("SECRET_GOLD", "SECRET_VERIFIER", "SYNTHETIC_KEY"))
    assert "SYNTHETIC_KEY" not in (tmp_path / "logs/live_run.json").read_text()
    with pytest.raises(adapter.BudgetStop, match="already consumed"):
        await agent.run("Inspect visible workspace", Environment(), AgentContext())
    assert provider.calls == 2


@pytest.mark.anyio
async def test_adapter_runs_mea_with_fixed_roles_and_read_only_auditor(
    tmp_path,
    monkeypatch,
):
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
                "finished_at": "2026-11-18",
            }
        )
    )

    manager_execute = json.dumps(
        {
            "next": "execute",
            "reason": "inspect workspace",
            "subtask": "run pwd and report it",
            "acceptance_criteria": ["pwd succeeds"],
            "question": "",
            "related_rounds": [1],
        }
    )
    manager_done = json.dumps(
        {
            "next": "done",
            "reason": "audit is complete",
            "subtask": "",
            "acceptance_criteria": [],
            "question": "",
            "related_rounds": [1],
        }
    )
    audit_complete = json.dumps(
        {
            "status": "complete",
            "summary": "workspace verified",
            "evidence": ["ls succeeded"],
            "verified_facts": ["pwd succeeds"],
            "remaining": [],
        }
    )

    class Provider:
        def __init__(self):
            self.calls = 0
            self.received = []
            self.responses = [
                Response(
                    model="fake",
                    content=manager_execute,
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                    outcome=TurnOutcome.COMPLETED,
                ),
                Response(
                    model="fake",
                    content="",
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                    tool_calls=[
                        {
                            "id": "executor-shell",
                            "type": "function",
                            "function": {
                                "name": "shell",
                                "arguments": '{"command":"pwd"}',
                            },
                        }
                    ],
                ),
                Response(
                    model="fake",
                    content="pwd completed",
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                    outcome=TurnOutcome.COMPLETED,
                ),
                Response(
                    model="fake",
                    content="",
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                    tool_calls=[
                        {
                            "id": "auditor-shell",
                            "type": "function",
                            "function": {
                                "name": "audit_shell",
                                "arguments": '{"command":"ls"}',
                            },
                        }
                    ],
                ),
                Response(
                    model="fake",
                    content=audit_complete,
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                    outcome=TurnOutcome.COMPLETED,
                ),
                Response(
                    model="fake",
                    content=manager_done,
                    usage={"prompt_tokens": 10, "completion_tokens": 2},
                    outcome=TurnOutcome.COMPLETED,
                ),
            ]

        async def achat(self, messages, **kwargs):
            self.received.extend(m.text_content or "" for m in messages)
            response = self.responses[self.calls]
            self.calls += 1
            return response

    provider = Provider()

    def client(config):
        llm = LLM(config)
        llm._provider = provider
        return llm

    monkeypatch.setattr(
        adapter,
        "create_live_llm",
        lambda **kw: client(
            Config(
                provider="openai",
                model="deepseek-v4.1-flash",
                api_key="SYNTHETIC_KEY",
                max_retries=0,
                max_tokens=kw["max_output_tokens"],
            )
        ),
    )
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_KEY")

    class Environment:
        def __init__(self):
            self.commands = []

        async def exec(self, command, timeout_sec):
            self.commands.append(command)
            return SimpleNamespace(
                return_code=0,
                stdout="visible.py\n",
                stderr="",
            )

    environment = Environment()
    agent = adapter.BudgetedHarborAgent(
        task_dir=task,
        logs_dir=tmp_path / "logs",
        oracle_result=oracle,
        ledger_path=tmp_path / "ledger.json",
        compaction_mode="layered",
        artifact_projection="on",
        orchestration_mode="mea",
        experiment_arm="mea",
        max_rounds=2,
        max_requests=64,
    )
    context = AgentContext()
    await agent.run("Inspect visible workspace", environment, context)

    assert environment.commands == ["pwd", "ls"]
    assert provider.calls == 6
    assert context.metadata["status"] == "complete"
    assert context.metadata["orchestration_mode"] == "mea"
    assert context.metadata["mea"]["rounds"] == 2
    assert context.metadata["limits"]["compaction_profile"]["micro_min_chars"] == 256
    assert context.metadata["limits"]["compaction_profile"]["recent_tool_groups"] == 1
    assert set(context.metadata["role_prompt_hashes"]) == {
        "manager",
        "executor",
        "auditor",
    }
    role_log = (tmp_path / "logs/quality_roles.jsonl").read_text()
    assert "mea_manager_r1" in role_log
    assert "mea_executor_r1" in role_log
    assert "mea_auditor_r1" in role_log
    assert "mea_manager_r2" in role_log
    assert not any(
        secret in "\n".join(provider.received)
        for secret in ("SECRET_GOLD", "SECRET_VERIFIER", "SYNTHETIC_KEY")
    )
