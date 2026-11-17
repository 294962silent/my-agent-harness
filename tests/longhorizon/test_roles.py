from __future__ import annotations

import asyncio
import contextvars
import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from my_agent_core.events import AgentEnd, MessageEnd, ToolExecutionStart
from my_agent_core.hooks import ToolCallHook
from my_agent_llm import Message
from my_agent_llm.models import normalize_finish_reason
from my_agent_llm.observation import CallObservation, CallStart
from my_coding_agent.longhorizon.roles import (
    AUDITOR_BASH_COMMANDS,
    AUDITOR_SYSTEM_PROMPT,
    AUDITOR_TOOL_NAMES,
    EXECUTOR_TOOL_NAMES,
    _ContextualLLM,
    _safe_auditor_bash_tool,
    LLMRoleBackend,
    ManagerDecision,
    RoleBackend,
    RoleProtocolError,
)


class FakeLLM:
    def __init__(
        self,
        *,
        tool_calls=None,
        content='{"next":"done","reason":"ok"}',
        finish_reason="stop",
    ) -> None:
        self.tool_calls = tool_calls
        self.content = content
        self.finish_reason = finish_reason
        self.calls = []
        self._observers = []

    def add_observer(self, observer):
        self._observers.append(observer)
        owner = self

        class Subscription:
            def close(self) -> None:
                if observer in owner._observers:
                    owner._observers.remove(observer)

        return Subscription()

    def _emit(self, record) -> None:
        for observer in tuple(self._observers):
            observer(record)

    async def achat(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools, "kwargs": kwargs})
        observation = CallObservation(
            self._emit,
            provider="fake",
            model="fake",
            method="achat",
        )
        try:
            return _response(
                content=self.content,
                tool_calls=self.tool_calls,
                finish_reason=self.finish_reason,
            )
        finally:
            observation.finish()

    async def achat_stream(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools, "kwargs": kwargs})
        observation = CallObservation(
            self._emit,
            provider="fake",
            model="fake",
            method="achat_stream",
        )
        try:
            yield SimpleNamespace(content="stream chunk")
            yield SimpleNamespace(content="stream chunk 2")
        finally:
            observation.finish()

    def stream(self, messages, *, tools=None, **kwargs):
        self.calls.append({"messages": list(messages), "tools": tools, "kwargs": kwargs})
        observation = CallObservation(
            self._emit,
            provider="fake",
            model="fake",
            method="stream",
        )
        try:
            yield SimpleNamespace(content="stream chunk")
            yield SimpleNamespace(content="stream chunk 2")
        finally:
            observation.finish()


class FakeAgent:
    instances = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.closed = False
        self.prompt = ""
        self.__class__.instances.append(self)

    async def prompt_stream(self, prompt):
        self.prompt = prompt
        async for _chunk in self.kwargs["llm"].achat_stream(
            [Message(role="user", content=prompt)],
            tools=[{"name": "probe"}],
        ):
            pass
        tool_name = (
            "write"
            if {tool.name for tool in self.kwargs["tools"]} & {"write"}
            else "read"
        )
        message = Message(
            role="assistant",
            content=f"{tool_name} completed",
            metadata={"usage": {"prompt_tokens": 5, "completion_tokens": 2}},
        )
        yield MessageEnd(message=message)
        yield ToolExecutionStart(tool_call_id="tool-1", tool_name=tool_name, args={})
        yield AgentEnd(
            messages=[message],
            final_text=f"{tool_name} completed",
            iterations=2,
            stop_reason="end_turn",
        )

    async def aclose(self) -> None:
        self.closed = True


def _response(*, content: str, finish_reason: str | None, tool_calls=None):
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        usage={"prompt_tokens": 7, "completion_tokens": 3},
        finish_reason=finish_reason,
        outcome=normalize_finish_reason(finish_reason, bool(tool_calls)),
    )


@pytest.fixture(autouse=True)
def reset_fake_agents():
    FakeAgent.instances = []


def test_manager_uses_direct_llm_with_no_tools_and_records_prompt(tmp_path):
    llm = FakeLLM()
    backend = LLMRoleBackend(llm=llm, workspace=tmp_path, run_dir=tmp_path / "run")

    episode = asyncio.run(
        backend.manager(task="fix files", state={"verified": ["a"]}, round_index=1)
    )

    assert len(llm.calls) == 1
    assert llm.calls[0]["tools"] == []
    assert episode.output == '{"next":"done","reason":"ok"}'
    assert episode.terminal == "end_turn"
    assert episode.tool_names == ()
    assert episode.usage == ({"prompt_tokens": 7, "completion_tokens": 3},)
    assert episode.session_path.endswith("round_0001_manager.jsonl")
    assert json.loads(
        Path(episode.session_path).read_text(encoding="utf-8")
    )["output"] == episode.output
    assert episode.cleanup_ok is True
    prompt = (tmp_path / "run" / "prompts" / "round_0001_manager.txt").read_text(encoding="utf-8")
    assert "fix files" in prompt
    assert '"verified"' in prompt


def test_manager_rejects_tool_calls(tmp_path):
    backend = LLMRoleBackend(
        llm=FakeLLM(tool_calls=[{"name": "bash"}]),
        workspace=tmp_path,
        run_dir=tmp_path / "run",
    )
    with pytest.raises(RoleProtocolError, match="tool"):
        asyncio.run(backend.manager(task="fix", state={}, round_index=1))


def test_manager_rejects_non_completed_outcome(tmp_path):
    llm = FakeLLM(content='{"next":"done","reason":"truncated"}', finish_reason="length")
    backend = LLMRoleBackend(llm=llm, workspace=tmp_path, run_dir=tmp_path / "run")
    with pytest.raises(RoleProtocolError, match="non-completed outcome: length"):
        asyncio.run(backend.manager(task="fix", state={}, round_index=1))


def test_executor_and_auditor_use_fresh_isolated_agents_and_sessions(tmp_path):
    auditor_workspace = tmp_path / "auditor-copy"
    auditor_workspace.mkdir()
    backend = LLMRoleBackend(
        llm=FakeLLM(),
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        agent_factory=FakeAgent,
        limits={"max_turns": 3, "max_seconds": 9},
    )
    decision = ManagerDecision(
        next="execute",
        reason="change the CLI",
        subtask="edit main.py",
        acceptance_criteria=("command succeeds",),
    )

    executor = asyncio.run(
        backend.executor(
            task="fix files",
            decision=decision,
            state={"verified": ["baseline"]},
            round_index=2,
        )
    )
    auditor = asyncio.run(
        backend.auditor(
            task="fix files",
            decision=decision,
            executor_output=executor.output,
            state={"verified": ["baseline"]},
            round_index=2,
            workspace=auditor_workspace,
        )
    )

    assert len(FakeAgent.instances) == 2
    executor_agent, auditor_agent = FakeAgent.instances
    assert executor_agent is not auditor_agent
    assert executor.session_path != auditor.session_path
    assert executor_agent.kwargs["session"] is not auditor_agent.kwargs["session"]
    assert executor_agent.kwargs["session"].cwd == str(tmp_path.resolve())
    assert auditor_agent.kwargs["session"].cwd == str(auditor_workspace.resolve())

    executor_tools = {tool.name for tool in executor_agent.kwargs["tools"]}
    auditor_tools = {tool.name for tool in auditor_agent.kwargs["tools"]}
    assert executor_tools == set(EXECUTOR_TOOL_NAMES)
    assert auditor_tools == set(AUDITOR_TOOL_NAMES)
    assert {"write", "edit", "bash", "artifact_read", "artifact_search"} <= executor_tools
    assert auditor_tools == {"read", "grep", "find", "ls", "bash"}
    assert not auditor_tools & {"write", "edit", "artifact_read", "artifact_search"}

    for agent in (executor_agent, auditor_agent):
        kwargs = agent.kwargs
        assert kwargs["skill_dirs"] == []
        assert kwargs["subagent_dirs"] == []
        assert kwargs["extension_dirs"] == []
        assert kwargs["plugin_dirs"] == []
        assert kwargs["memory_dir"] is False
        assert kwargs["task_store"] is False
        assert kwargs["run_limits"].max_turns == 3
        assert kwargs["run_limits"].max_seconds == 9
        assert agent.closed is True

    auditor_hooks = auditor_agent.kwargs["hooks"]
    assert len(auditor_hooks) == 1 and auditor_hooks[0][0] is ToolCallHook
    gate = auditor_hooks[0][1]
    assert gate.mode == "review"
    assert gate.allow_without_callback is False
    assert gate.workspace == auditor_workspace.resolve()
    blocked = asyncio.run(gate(ToolCallHook("unsafe", "bash", {"command": "rm -rf build"})))
    safe = asyncio.run(gate(ToolCallHook("safe", "bash", {"command": "git status --short"})))
    assert blocked.block is True
    assert safe.block is False

    assert executor.tool_names == ("write",)
    assert auditor.tool_names == ("read",)
    assert executor.usage == ({"prompt_tokens": 5, "completion_tokens": 2},)
    assert executor.terminal == "end_turn" and executor.iterations == 2
    assert executor.cleanup_ok is True and auditor.cleanup_ok is True
    assert (tmp_path / "run" / "prompts" / "round_0002_executor.txt").is_file()
    assert (tmp_path / "run" / "prompts" / "round_0002_auditor.txt").is_file()
    assert (tmp_path / "run" / "cleanup" / "round_0002_executor.json").is_file()
    assert (tmp_path / "run" / "cleanup" / "round_0002_auditor.json").is_file()
    assert json.loads(
        (tmp_path / "run" / "cleanup" / "round_0002_auditor.json").read_text(encoding="utf-8")
    )["ok"] is True


def test_auditor_protocol_accepts_optional_workspace():
    parameter = inspect.signature(RoleBackend.auditor).parameters["workspace"]

    assert parameter.default is None


def test_all_roles_share_the_run_trace_root_and_use_stable_purposes(tmp_path):
    llm = FakeLLM()
    records = []
    llm.add_observer(records.append)
    backend = LLMRoleBackend(
        llm=llm,
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        agent_factory=FakeAgent,
    )
    decision = ManagerDecision(
        next="execute",
        reason="edit",
        subtask="edit the file",
        acceptance_criteria=("file is correct",),
    )

    asyncio.run(backend.manager(task="fix", state={}, round_index=4))
    asyncio.run(
        backend.executor(
            task="fix",
            decision=decision,
            state={},
            round_index=4,
        )
    )
    asyncio.run(
        backend.auditor(
            task="fix",
            decision=decision,
            executor_output="changed",
            state={},
            round_index=4,
        )
    )

    starts = [record for record in records if type(record) is CallStart]
    assert [record.root_id for record in starts] == ["run", "run", "run"]
    assert [record.purpose for record in starts] == [
        "longhorizon_manager_r4",
        "longhorizon_executor_r4",
        "longhorizon_auditor_r4",
    ]


def test_stream_wrappers_do_not_carry_contextvar_tokens_across_contexts(tmp_path):
    llm = FakeLLM()
    records = []
    llm.add_observer(records.append)
    contextual = _ContextualLLM(
        llm,
        root_id=tmp_path.name,
        purpose="longhorizon_executor_r7",
    )

    async def consume_async():
        stream = contextual.achat_stream([], tools=[])
        loop = asyncio.get_running_loop()
        first_context = contextvars.copy_context()
        second_context = contextvars.copy_context()
        first = await first_context.run(lambda: loop.create_task(anext(stream)))
        second = await second_context.run(lambda: loop.create_task(anext(stream)))
        await stream.aclose()
        return first, second

    first, second = asyncio.run(consume_async())
    assert first.content == "stream chunk"
    assert second.content == "stream chunk 2"

    sync_stream = contextual.stream([], tools=[])
    first_context = contextvars.copy_context()
    second_context = contextvars.copy_context()
    assert first_context.run(lambda: next(sync_stream)).content == "stream chunk"
    assert second_context.run(lambda: next(sync_stream)).content == "stream chunk 2"
    sync_stream.close()

    starts = [record for record in records if type(record) is CallStart]
    assert {record.root_id for record in starts} == {tmp_path.name}
    assert {record.purpose for record in starts} == {"longhorizon_executor_r7"}


def test_agent_cleanup_failure_is_returned_as_evidence(tmp_path):
    class FailingCleanupAgent(FakeAgent):
        async def aclose(self) -> None:
            self.closed = True
            raise RuntimeError("cleanup exploded")

    backend = LLMRoleBackend(
        llm=FakeLLM(),
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        agent_factory=FailingCleanupAgent,
    )
    episode = asyncio.run(
        backend.executor(
            task="fix",
            decision=ManagerDecision(
                next="execute",
                reason="edit",
                subtask="edit",
                acceptance_criteria=("works",),
            ),
            state={},
            round_index=1,
        )
    )
    assert episode.cleanup_ok is False
    evidence = json.loads(
        (tmp_path / "run" / "cleanup" / "round_0001_executor.json").read_text(encoding="utf-8")
    )
    assert evidence["ok"] is False
    assert "RuntimeError" in evidence["errors"]


def test_role_execution_failure_writes_safe_diagnostic_and_protocol_detail(tmp_path):
    role_secret = "sk-" + "role-secret-123456"
    bearer_secret = "sk-" + "bearer-secret-123456"
    config_secret = "sk-" + "config-secret-123456"

    class FailingAgent(FakeAgent):
        async def prompt_stream(self, prompt):
            if False:
                yield
            raise TypeError(
                "'int' object is not iterable "
                f"api_key={role_secret} "
                f"Authorization: Bearer {bearer_secret}"
            )

    llm = FakeLLM()
    llm.config = SimpleNamespace(api_key=config_secret)
    backend = LLMRoleBackend(
        llm=llm,
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        agent_factory=FailingAgent,
    )

    with pytest.raises(RoleProtocolError) as caught:
        asyncio.run(
            backend.executor(
                task="fix",
                decision=ManagerDecision(
                    next="execute",
                    reason="edit",
                    subtask="edit",
                    acceptance_criteria=("works",),
                ),
                state={},
                round_index=3,
            )
        )

    error = json.loads(
        (tmp_path / "run" / "errors" / "round_0003_executor.json").read_text(
            encoding="utf-8"
        )
    )
    assert error["error_type"] == "TypeError"
    assert "'int' object is not iterable" in error["message"]
    assert len(error["message"]) <= 1000
    assert error["cleanup_ok"] is True
    assert error["errors"] == []
    serialized = json.dumps(error)
    for secret in (
        role_secret,
        bearer_secret,
        config_secret,
    ):
        assert secret not in serialized
    assert "Traceback" not in serialized
    assert "TypeError" in str(caught.value)
    assert "'int' object is not iterable" in str(caught.value)
    assert "sk-role-secret" not in str(caught.value)


def test_role_execution_failure_records_cleanup_failure_and_truncates_message(tmp_path):
    class FailingAgent(FakeAgent):
        async def prompt_stream(self, prompt):
            if False:
                yield
            raise ValueError("x" * 1500)

        async def aclose(self):
            self.closed = True
            raise RuntimeError("cleanup failed")

    backend = LLMRoleBackend(
        llm=FakeLLM(),
        workspace=tmp_path,
        run_dir=tmp_path / "run",
        agent_factory=FailingAgent,
    )
    with pytest.raises(RoleProtocolError) as caught:
        asyncio.run(
            backend.auditor(
                task="fix",
                decision=ManagerDecision(
                    next="execute",
                    reason="audit",
                    subtask="verify",
                    acceptance_criteria=("works",),
                ),
                executor_output="claimed",
                state={},
                round_index=8,
            )
        )

    error = json.loads(
        (tmp_path / "run" / "errors" / "round_0008_auditor.json").read_text(
            encoding="utf-8"
        )
    )
    assert error["error_type"] == "ValueError"
    assert error["message"].endswith("...")
    assert len(error["message"]) == 1000
    assert error["cleanup_ok"] is False
    assert error["errors"] == ["RuntimeError"]
    message = str(caught.value)
    assert "ValueError" in message
    assert "cleanup_ok=False" in message
    assert "RuntimeError" in message


def test_auditor_prompt_forbids_shell_meta_commands_and_requests_immediate_json():
    prompt = AUDITOR_SYSTEM_PROMPT
    normalized = " ".join(prompt.split())

    assert (
        "exactly one standalone command with no arguments, shell metacharacters"
        in normalized
    )
    assert "`python -m pytest -q`" in normalized
    assert "`uv run pytest -q`" in normalized
    for forbidden in ("pipes", "redirection", "semicolons", "`&&`", "`cd`"):
        assert forbidden in normalized
    assert "Use read/grep/find/ls for inspection" in normalized
    assert "return the JSON object immediately" in normalized
    assert "Do not continue exploring." in normalized
    assert "status=complete requires remaining=[]" in normalized
    assert "Put nonblocking caveats in summary or evidence, never in remaining" in normalized
    assert "If any acceptance criterion is unmet" in normalized
    assert "use status=incomplete or status=blocked" in normalized


def test_auditor_bash_accepts_only_exact_pytest_commands(tmp_path):
    calls = []

    async def underlying(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(ok=True, data="ok", error=None)

    bash = _safe_auditor_bash_tool(tmp_path, underlying)
    for command in AUDITOR_BASH_COMMANDS:
        result = asyncio.run(bash(command))
        assert result.ok is True
    assert [command for command, _kwargs in calls] == list(AUDITOR_BASH_COMMANDS)
    assert all(
        kwargs["run_in_background"] is False
        for _command, kwargs in calls
    )

    escapes = (
        "python -m pytest -q --rootdir=.",
        "python -m pytest -q -c hidden.ini",
        "python -m pytest -q 2>&1 | tail",
        "python -m pytest -q > result.txt",
        "python -m pytest -q; echo ok",
        "python -m pytest -q && echo ok",
        "cd . && python -m pytest -q",
        " python -m pytest -q",
        "python -m pytest -q ",
        "uv run pytest -q --config=hidden",
        "pytest -q",
        "python -m pytest -q\nrm -rf .",
    )
    before = len(calls)
    for command in escapes:
        result = asyncio.run(bash(command))
        assert result.ok is False
    assert len(calls) == before

    with pytest.raises(ValueError):
        bash.params_model.model_validate(
            {"command": "python -m pytest -q", "rootdir": "."}
        )
