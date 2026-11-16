"""Small deterministic fixtures: real production APIs, explicitly scoped comparisons."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
from typing import Callable

from my_agent_llm import Message, Response


@dataclass
class Case:
    module: str
    name: str
    arms: tuple[str, ...]
    kind: str
    run: Callable
    paths: list[str]
    intervention: str


async def tools(arm, path):
    from my_agent_core.registry import ToolRegistry
    from my_agent_core.tools import tool

    registry, active, peak, starts = ToolRegistry(), 0, 0, []

    @tool(is_parallel_safe=arm == "on")
    async def inspect_file(name: str) -> str:
        nonlocal active, peak
        active += 1
        peak = max(active, peak)
        starts.append(name)
        await asyncio.sleep(0.01)
        active -= 1
        return name

    registry.register(inspect_file)
    results = await registry.execute_batch([("inspect_file", {"name": name}) for name in ("a", "b", "c")])
    return (
        dict(peak_concurrency=peak, completed=len(results)),
        {
            "all_completed": all(r.ok for r in results),
            "correct_overlap": peak == (3 if arm == "on" else 1),
            "input_order_preserved": [r.data for r in results] == ["a", "b", "c"],
        },
        dict(start_order=starts, returned=[r.data for r in results]),
    )


async def dependency(arm, path):
    from my_agent_core.registry import ToolRegistry
    from my_agent_core.tools import tool

    registry, events = ToolRegistry(), []

    @tool(is_parallel_safe=False)
    async def write() -> str:
        await asyncio.sleep(0.01)
        (path / "state").write_text("committed")
        events.append("write_committed")
        return "written"

    @tool(is_parallel_safe=True)
    async def read() -> str:
        value = (path / "state").read_text() if (path / "state").exists() else "missing"
        events.append("read_" + value)
        return value

    registry.register(write)
    registry.register(read)
    calls = [("write", {}), ("read", {})]
    if arm == "on":
        results = await registry.execute_batch(calls)
    else:
        # Explicit naive comparison wrapper; not an unsupported dependency toggle.
        results = await asyncio.gather(*(registry.execute_tool(c) for c in calls))
    violation = int(results[1].data != "committed")
    return (
        dict(dependency_violations=violation),
        {"expected_intervention_effect": violation == int(arm == "off")},
        dict(events=events, read_value=results[1].data),
    )


async def session(arm, path):
    from my_agent_core.session import Session

    store = Session(path=path / "session.jsonl")
    root = store.add_message("user", "root")
    store.add_message("assistant", "branch_A")
    store.rewind(root.id)
    store.add_message("assistant", "branch_B")
    loaded = Session.load(store.path)
    if arm == "on":
        messages = loaded.get_current_path_messages()
    else:
        # Flat append-log projection intentionally ignores the selected branch.
        messages = [
            Message(role=entry.role, content=entry.content)
            for entry in loaded.tree.entries.values()
            if hasattr(entry, "role") and hasattr(entry, "content")
        ]
    texts = [message.text_content for message in messages]
    leaks = sum(text == "branch_A" for text in texts)
    return (
        dict(branch_leaks=leaks, messages=len(texts)),
        {
            "selected_branch_retained": "branch_B" in texts,
            "expected_isolation": leaks == int(arm == "off"),
        },
        dict(restored=texts),
    )


async def context(arm, path):
    from my_agent_core.artifacts import ArtifactStore
    from my_agent_core.context import ContextManager

    body = "header\n" + "log abc 汉字\n" * 8000 + "ERROR_AT_END"
    store = ArtifactStore(path)
    messages = [
        Message(role="user", content="inspect log"),
        Message(role="tool", content=body, metadata={"tool_name": "bash", "tool_call_id": "call-log"}),
    ]

    class NoModel:
        async def achat(self, *args, **kwargs):
            raise AssertionError("offline context fixture must not invoke summarizer")

    manager = ContextManager(llm=NoModel(), budget=1_000_000, artifact_store=store if arm == "on" else None)
    view = await manager.prepare(messages)
    rendered = view[-1].text_content or ""
    handle = (view[-1].metadata or {}).get("artifact_id")
    recovered = ""
    if handle:
        cursor = 0
        while cursor is not None:
            page = store.read_page(handle, cursor=cursor, max_bytes=4096)
            recovered += page["text"]
            cursor = page["next_cursor"]
    available = recovered if handle else rendered
    return (
        dict(
            source_bytes=len(body.encode()),
            view_bytes=len(rendered.encode()),
            artifact_readback_supported=int(bool(handle)),
            artifact_readback_matches_source=int(recovered == body) if handle else None,
            evidence_recoverable=int(available == body),
            view_contains_full_source=int(rendered == body),
            artifact_disk_bytes=sum(p.stat().st_size for p in path.rglob('*') if p.is_file()),
            handles=int(bool(handle)),
        ),
        {
            "session_source_unchanged": messages[-1].content == body,
            "expected_handle": bool(handle) == (arm == "on"),
            "full_readback_when_enabled": recovered == body if arm == "on" else True,
            "tail_visible_when_enabled": "ERROR_AT_END" in rendered if arm == "on" else True,
        },
        dict(
            source_sha256=hashlib.sha256(body.encode()).hexdigest(),
            recovered_sha256=hashlib.sha256(recovered.encode()).hexdigest() if handle else None,
        ),
    )


async def summary_binding(arm, path):
    from my_agent_core.context import ContextManager

    class SummaryModel:
        calls = 0
        async def achat(self, **kwargs):
            self.calls += 1
            return Response(content='BOUND_OLD_SUMMARY', model='offline-fixture')

    llm = SummaryModel()
    manager = ContextManager(llm=llm, budget=100_000, keep_recent_tokens=1)
    history = [Message(role='user', content='old prefix'), Message(role='assistant', content='answer'),
               Message(role='user', content='tail'), Message(role='assistant', content='tail answer')]
    await manager.force_compact(history)
    stable = await manager.prepare(history)
    changed = [Message(role='user', content='changed prefix'), *history[1:]]
    rebuilt = await manager.prepare(changed)
    leaks = sum('BOUND_OLD_SUMMARY' in (m.text_content or '') for m in rebuilt)
    return (dict(changed_source_summary_leaks=leaks, summarizer_calls=llm.calls,
                 unchanged_source_summary_reused=int(any('BOUND_OLD_SUMMARY' in (m.text_content or '') for m in stable))),
            dict(changed_source_visible=rebuilt == changed, no_stale_summary=leaks == 0,
                 no_extra_model_request=llm.calls == 1), dict(changed_prefix='changed prefix'))


async def memory(arm, path):
    from my_agent_core.memory import MemoryStore

    store = MemoryStore(path)
    store.load_from_disk()
    store.remember("preferred_editor", "vim")
    store.remember("preferred_editor", "neovim")
    store.remember("service_port", "8080")
    queries = [("preferred editor", "preferred_editor"), ("service port", "service_port")]
    actual = []
    for query, expected in queries:
        text = store.retrieve(query) if arm == "on" else None
        actual.append(dict(query=query, expected=expected, retrieved=text, hit=expected in (text or "")))
    stale = sum("value=vim" in (r["retrieved"] or "") for r in actual)
    hits = sum(r["hit"] for r in actual)
    return (
        dict(recall=hits / len(queries), hits=hits, labeled_queries=len(queries), stale_hits=stale),
        {
            "expected_retrieval": hits == (len(queries) if arm == "on" else 0),
            "no_stale_facts": stale == 0,
        },
        dict(labels=actual),
    )


async def permission(arm, path):
    from my_agent_core.agent import Agent
    from my_agent_core.session import Session
    from my_agent_core.tools import tool
    from my_coding_agent.permissions import PermissionGate
    from my_agent_core.hooks import ToolCallHook

    writes = []

    @tool
    def restricted_write() -> str:
        (path / "side_effect").write_text("executed")
        writes.append("executed")
        return "written"

    class ToolModel:
        def __init__(self):
            self.calls = 0

        async def achat_stream(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                yield Response(
                    model="offline-fixture",
                    content="",
                    tool_calls=[
                        {"id": "w", "type": "function", "function": {"name": "restricted_write", "arguments": "{}"}}
                    ],
                )
            else:
                yield Response(model="offline-fixture", content="done")

    agent = Agent(
        llm=ToolModel(),
        session=Session(path=path / "session.jsonl"),
        tools=[restricted_write],
        system_prompt="offline",
        memory_dir=False,
    )
    agent.hooks.register(ToolCallHook, PermissionGate(mode="review" if arm == "on" else "autonomous", workspace=path))
    try:
        await agent.run("perform write")
    finally:
        await agent.aclose()
    count = len(writes)
    return (
        dict(unauthorized_side_effects=count),
        {"expected_gate_effect": count == int(arm == "off")},
        dict(file_exists=(path / "side_effect").exists()),
    )


async def runtime(arm, path):
    from my_agent_core.agent import Agent
    from my_agent_core.run_lifecycle import RunLimits
    from my_agent_core.session import Session
    from my_agent_core.tools import tool
    from my_agent_core.events import AgentEnd

    @tool
    def ping() -> str:
        return "pong"

    class FiniteModel:
        def __init__(self):
            self.calls = 0

        async def achat_stream(self, **kwargs):
            self.calls += 1
            if self.calls <= 4:
                yield Response(
                    model="offline-fixture",
                    content="",
                    tool_calls=[
                        {"id": f"c{self.calls}", "type": "function", "function": {"name": "ping", "arguments": "{}"}}
                    ],
                )
            else:
                yield Response(model="offline-fixture", content="done")

    model = FiniteModel()
    agent = Agent(
        llm=model,
        session=Session(path=path / "s.jsonl"),
        tools=[ping],
        memory_dir=False,
        run_limits=RunLimits(max_turns=2) if arm == "on" else RunLimits(),
    )
    try:
        events = [e async for e in agent.prompt_stream("go")]
    finally:
        await agent.aclose()
    end = next(e for e in reversed(events) if isinstance(e, AgentEnd))
    return (
        dict(model_calls=model.calls, budget_exhausted=int(end.stop_reason == "budget_exhausted")),
        {
            "expected_model_calls": model.calls == (2 if arm == "on" else 5),
            "expected_stop": end.stop_reason == ("budget_exhausted" if arm == "on" else "end_turn"),
        },
        dict(stop_reason=end.stop_reason, limit_details=end.limit_details, run_id_present=bool(end.run_id)),
    )


def cases():
    return [
        Case(
            "tools",
            "safe_parallel",
            ("on", "off"),
            "ablation",
            tools,
            ["src/my_agent_core/registry.py:ToolRegistry.execute_batch"],
            "is_parallel_safe True versus False, same tools and calls",
        ),
        Case(
            "tools",
            "write_read_dependency",
            ("on", "off"),
            "comparison",
            dependency,
            ["src/my_agent_core/registry.py:ToolRegistry.execute_batch"],
            "production unsafe-batch serialization versus explicit naive gather wrapper; no dependency-DAG claim",
        ),
        Case(
            "session",
            "branch_restore",
            ("on", "off"),
            "comparison",
            session,
            ["src/my_agent_core/session/session.py:Session.load,rewind"],
            "selected-path projection versus explicit flat entry projection, same persisted tree",
        ),
        Case(
            "context",
            "artifact_roundtrip",
            ("on", "off"),
            "ablation",
            context,
            [
                "src/my_agent_core/context.py:ContextManager.prepare",
                "src/my_agent_core/artifacts.py:ArtifactStore.read_page",
            ],
            "artifact_store enabled versus None, same canonical messages and budget",
        ),
        Case(
            'context', 'summary_source_binding', ('contract',), 'contract', summary_binding,
            ['src/my_agent_core/context.py:ContextManager.force_compact,prepare'],
            'unchanged source reuses summary; edited covered prefix invalidates cached summary without another model call',
        ),
        Case(
            "memory",
            "labeled_retrieval",
            ("on", "off"),
            "ablation",
            memory,
            ["src/my_agent_core/memory.py:MemoryStore.remember,retrieve"],
            "query-time memory injection enabled versus disabled, same facts and labels; fixture recall only",
        ),
        Case(
            "permission",
            "unauthorized_write",
            ("on", "off"),
            "ablation",
            permission,
            ["src/my_agent_core/agent.py:Agent.run", "src/my_coding_agent/permissions.py:PermissionGate"],
            "review versus autonomous explicit mode, isolated synthetic file",
        ),
        Case(
            "runtime",
            "turn_budget",
            ("on", "off"),
            "ablation",
            runtime,
            ["src/my_agent_core/agent.py:Agent.prompt_stream", "src/my_agent_core/run_lifecycle.py:RunLimits"],
            "max_turns=2 versus unbounded; finite fake model gives both arms a defined ending",
        ),
    ] + extended_cases()


def extended_cases():
    from .model_cases import model_cases

    return model_cases()
