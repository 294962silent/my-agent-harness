"""RSI uses production agents and actual tools; scripted model quality is unmeasured."""
import asyncio

import pytest

from my_agent_llm import Config, Response
from my_agent_llm.observation import observation_context
from my_coding_agent.rsi_runtime import ParentBudget, RsiBudgetExceeded, SharedLLM


class ScriptedLLM:
    config = Config(provider="openai", model="scripted-contract", max_tokens=64, max_retries=0)
    model = "scripted-contract"
    calls = 0

    async def achat(self, messages, **kwargs):
        self.calls += 1
        return Response(model=self.model, content="done", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})


def test_shared_parent_counts_stream_summary_and_evaluator_together():
    async def exercise():
        raw = ScriptedLLM()
        budget = ParentBudget(max_requests=2, max_tokens=10000, max_seconds=30, max_evaluations=2)
        llm = SharedLLM(raw, budget)
        async for _ in llm.astream_events([]):
            pass
        with observation_context(purpose="summary"):
            await llm.achat([])
        with pytest.raises(RsiBudgetExceeded):
            await llm.achat([], purpose="visible_judge")
        assert raw.calls == 2
        assert budget.snapshot()["requests"] == 2
        assert budget.snapshot()["used_tokens"] == 10
        assert budget.records[1]["purpose"] == "summary"
        assert not hasattr(llm, "_provider")
        with pytest.raises(NotImplementedError):
            llm.chat([])
    asyncio.run(exercise())


@pytest.mark.parametrize("failure", ["usage", "cancel"])
def test_unknown_or_cancelled_call_retains_reservation_and_blocks_every_branch(failure):
    class Broken(ScriptedLLM):
        async def achat(self, messages, **kwargs):
            self.calls += 1
            if failure == "cancel":
                raise asyncio.CancelledError
            return Response(model=self.model, content="done", usage=None)

    async def exercise():
        budget = ParentBudget(max_requests=5, max_tokens=10000, max_seconds=30, max_evaluations=2)
        raw, llm = Broken(), None
        llm = SharedLLM(raw, budget)
        with pytest.raises((RsiBudgetExceeded, asyncio.CancelledError)):
            await llm.achat([])
        assert budget.snapshot()["held_tokens"] > 0 and budget.snapshot()["blocked"]
        with pytest.raises(RsiBudgetExceeded):
            await SharedLLM(raw, budget).achat([])
        assert raw.calls == 1
    asyncio.run(exercise())


def test_core_compaction_policy_keeps_same_shared_llm(tmp_path):
    from my_agent_core.agent import Agent
    from my_agent_core.context_compaction import CompactionPolicy
    from my_agent_core.session import Session
    budget = ParentBudget(max_requests=2, max_tokens=10000, max_seconds=30, max_evaluations=2)
    shared = SharedLLM(ScriptedLLM(), budget)
    policy = CompactionPolicy(mode="layered", model_context_window=32000)
    agent = Agent(llm=shared, tools=[], session=Session(path=tmp_path / "session.jsonl"),
                  memory_dir=False, task_store=False, skill_dirs=[], subagent_dirs=[], extension_dirs=[],
                  plugin_dirs=[], compaction_policy=policy)
    assert agent.context_manager.compaction_policy is policy
    assert agent.context_manager.llm is shared
    asyncio.run(agent.aclose())


@pytest.mark.parametrize("mode", ["baseline", "layered"])
def test_coding_agent_executes_isolated_real_branches_and_exports_measured_trace(tmp_path, mode):
    import json
    import sys
    from my_agent_core.session import Session
    from my_agent_core.context_compaction import CompactionPolicy
    from my_coding_agent.agent import CodingAgent
    from my_coding_agent.dream_rsi_contract import validate_trace
    from my_coding_agent.rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
    from my_coding_agent.rsi_runtime import FrozenPolicy, RsiRuntime, _SCOPE

    class Writer(ScriptedLLM):
        async def achat(self, messages, **kwargs):
            self.calls += 1
            if messages[-1].role == "tool":
                return Response(model=self.model, content="edited", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5})
            scope = _SCOPE.get()
            answer = "42" if scope["branch"] == 1 or scope["attempt"] > 0 else "7"
            return Response(model=self.model, content="", usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
                tool_calls=[{"id": "write-" + str(self.calls), "type": "function", "function": {
                    "name": "write", "arguments": json.dumps({"path": "answer.txt", "content": answer})}}])

    source = tmp_path / "original"
    source.mkdir()
    (source / "answer.txt").write_text("0")
    budget = ParentBudget(max_requests=8, max_tokens=100000, max_seconds=60, max_evaluations=5)
    factory = LocalBranchFactory(source, tmp_path / "rsi-output", attempt_max_turns=2,
                                 compaction_policy=CompactionPolicy(mode=mode, model_context_window=32000))
    evaluator = VisibleCommandEvaluator((sys.executable, "-c",
        "from pathlib import Path; import sys; sys.exit(0 if Path('answer.txt').read_text()=='42' else 1)"))
    runtime = RsiRuntime(factory=factory, evaluator=evaluator, budget=budget,
                         policy=FrozenPolicy({"max_width": 2, "max_refine": 1}), max_attempts=4,
                         task_id="real-file-contract", split="train")
    agent = CodingAgent(workspace=source, llm=Writer(), session=Session(path=tmp_path / "parent.jsonl"),
        rsi_runtime=runtime, memory_dir=False, task_store=False, auto_load_mcp=False,
        skill_dirs=[], subagent_dirs=[], extension_dirs=[], plugin_dirs=[], system_prompt="Write answer.txt")
    async def exercise():
        await agent.run("Make the visible test pass")
        await agent.aclose()
    asyncio.run(exercise())
    result = agent.last_rsi_result
    assert (source / "answer.txt").read_text() == "0"
    assert result.selected_visible_score == 1 and result.final_success is None
    assert result.report["provenance"]["kind"] == "synthetic"
    assert result.report["budget"]["requests"] == 8
    assert result.report["budget"]["evaluations"] == 5
    assert result.report["auto_promoted"] is False
    assert all(a["context"]["mode"] == mode for a in result.report["attempts"])
    if mode == "layered":
        assert any(a["context_activity"]["phase_fold_views"] for a in result.report["attempts"])
    trace = json.loads(result.trace_path.read_text())
    assert validate_trace(trace) == {"nodes": 4, "branches": 2, "max_attempt": 1}
    assert [n["id"] for n in trace["nodes"]] == ["b0_a0", "b1_a0", "b0_a1", "b1_a1"]
    assert (result.selected_path / "answer.txt").read_text() == "42"
    result.apply_to(source)
    assert (source / "answer.txt").read_text() == "42"


def reserved_budget(usage, *, max_output=256):
    from my_agent_llm import Message
    budget = ParentBudget(max_requests=4, max_tokens=10000, max_seconds=30, max_evaluations=2,
                          input_usd_per_million=10, output_usd_per_million=20)
    record = budget.reserve([Message(role="user", content="x" * 2000)], None, max_output, "agent")
    budget.settle(record, usage)
    return budget, record


def test_complete_usage_counts_separate_cache_using_authoritative_total():
    budget, record = reserved_budget({"prompt_tokens": 962, "completion_tokens": 195,
                                     "total_tokens": 1413, "cache_read_tokens": 256})
    assert record["input_tokens"] == 1218 and record["used_tokens"] == 1413
    assert record["cost_usd"] == pytest.approx((1218 * 10 + 195 * 20) / 1e6)
    assert "cache_write_tokens" not in record["usage"]
    assert budget.snapshot()["held_tokens"] == 0


def test_missing_total_is_determinate_only_when_all_normalized_input_components_are_explicit():
    budget, record = reserved_budget({"prompt_tokens": 10, "cache_read_tokens": 2,
                                     "cache_write_tokens": 1, "completion_tokens": 3})
    assert record["input_tokens"] == 13 and budget.snapshot()["used_tokens"] == 16
    for usage in [{"prompt_tokens": 10, "completion_tokens": 3},
                  {"prompt_tokens": 10, "cache_read_tokens": 2, "completion_tokens": 3}]:
        with pytest.raises(RsiBudgetExceeded):
            reserved_budget(usage)


@pytest.mark.parametrize("usage", [
    {"prompt_tokens": True, "completion_tokens": 1, "total_tokens": 2},
    {"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 2},
    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": "2"},
    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cache_read_tokens": -1},
    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cache_read_tokens": 3},
])
def test_invalid_usage_retains_reservation_and_blocks(usage):
    budget = ParentBudget(max_requests=4, max_tokens=10000, max_seconds=30, max_evaluations=2)
    record = budget.reserve([], None, 64, "agent")
    with pytest.raises(RsiBudgetExceeded):
        budget.settle(record, usage)
    assert budget.snapshot()["held_tokens"] == record["reserved_tokens"] and budget.blocked


def test_output_policy_violation_keeps_known_settlement_and_stops_parent():
    budget = ParentBudget(max_requests=4, max_tokens=10000, max_seconds=30, max_evaluations=2)
    record = budget.reserve([], None, 64, "agent")
    with pytest.raises(RsiBudgetExceeded):
        budget.settle(record, {"prompt_tokens": 1, "completion_tokens": 65, "total_tokens": 66})
    assert record["status"] == "settled" and record["used_tokens"] == 66
    assert record["policy_violation"] == "completion_exceeds_request_maximum"
    assert budget.blocked and budget.snapshot()["held_tokens"] == 0


def test_rpc_settings_reach_normal_coding_agent_compaction(tmp_path):
    from my_agent_core.session import Session
    from my_coding_agent.agent import CodingAgent
    from my_coding_agent.rpc.session_handlers import SessionRpcMixin
    from my_coding_agent.settings import ContextCompactionSettings, Settings
    assert Settings().context_compaction.mode == "baseline"
    owner = SessionRpcMixin()
    owner.settings = Settings(context_compaction=ContextCompactionSettings(mode="layered", model_context_window=32000))
    options = owner._integration_options()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent = CodingAgent(workspace=workspace, llm=ScriptedLLM(), session=Session(path=tmp_path / "normal.jsonl"),
        auto_load_mcp=False, memory_dir=False, task_store=False, skill_dirs=[], subagent_dirs=[],
        extension_dirs=[], plugin_dirs=[], **options)
    assert agent.context_manager.compaction_policy.mode == "layered"
    assert agent.context_manager.compaction_policy.model_context_window == 32000
    asyncio.run(agent.aclose())
    for invalid in ({"mode": "invented"}, {"output_reserve": True}, {"safety_margin": -1},
                    {"mode": "layered", "model_context_window": 100, "output_reserve": 100}):
        with pytest.raises(ValueError):
            ContextCompactionSettings(**invalid)


def test_shared_llm_exposes_only_narrow_projection_marker():
    from types import SimpleNamespace
    reasons = []
    raw = ScriptedLLM()
    raw._provider = SimpleNamespace(request_projection=SimpleNamespace(mark_message_rewrite=reasons.append,
                                                                        dangerous_callable=lambda: None))
    shared = SharedLLM(raw, ParentBudget(max_requests=2, max_tokens=10000, max_seconds=30, max_evaluations=2))
    shared.request_projection.mark_message_rewrite("local_completed_prefix_fold")
    assert reasons == ["local_completed_prefix_fold"]
    assert not hasattr(shared.request_projection, "dangerous_callable") and not hasattr(shared, "_provider")
    assert SharedLLM(ScriptedLLM(), shared.budget).request_projection is None


@pytest.mark.parametrize("cancel_by", ["facade", "task"])
def test_coding_agent_abort_cancels_active_rsi_and_retains_unknown_usage(tmp_path, cancel_by):
    import json
    import sys
    from my_agent_core.session import Session
    from my_coding_agent.agent import CodingAgent
    from my_coding_agent.rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
    from my_coding_agent.rsi_runtime import FrozenPolicy, RsiRuntime

    async def exercise():
        entered = asyncio.Event()
        class Blocking(ScriptedLLM):
            async def achat(self, messages, **kwargs):
                entered.set()
                await asyncio.Event().wait()
        source = tmp_path / "source"
        source.mkdir()
        factory = LocalBranchFactory(source, tmp_path / "out")
        budget = ParentBudget(max_requests=4, max_tokens=100000, max_seconds=30, max_evaluations=3)
        runtime = RsiRuntime(factory=factory, evaluator=VisibleCommandEvaluator((sys.executable, "-c", "pass")),
            budget=budget, policy=FrozenPolicy({"max_width": 1, "max_refine": 0}), task_id="cancel", split="test")
        agent = CodingAgent(workspace=source, llm=Blocking(), session=Session(path=tmp_path / "parent.jsonl"),
            rsi_runtime=runtime, auto_load_mcp=False, memory_dir=False, task_store=False,
            skill_dirs=[], subagent_dirs=[], extension_dirs=[], plugin_dirs=[])
        task = asyncio.create_task(agent.run("cancel contract"))
        await asyncio.wait_for(entered.wait(), timeout=10)
        agent.abort() if cancel_by == "facade" else task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        report = json.loads((factory.output / "result.json").read_text())
        assert report["status"] == "cancelled" and report["trace_sha256"] is None
        assert report["invalid_attempts"] == 1 and report["completed_attempts"] == 0
        assert budget.blocked and budget.snapshot()["held_tokens"] > 0
        assert runtime._task is None
        await agent.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("mutation", ["source", "candidate", "manifest"])
def test_explicit_apply_refuses_changed_evidence(tmp_path, mutation):
    from my_coding_agent.rsi_branch import LocalBranchFactory
    from my_coding_agent.rsi_runtime import RsiResult
    source = tmp_path / "source"
    source.mkdir()
    (source / "answer.txt").write_text("0")
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "answer.txt").write_text("42")
    factory = LocalBranchFactory(source, tmp_path / "out")
    selected, patch, changes = factory.export(candidate)
    assert "-0\n\\ No newline at end of file\n+42\n" in patch.read_text()
    result = RsiResult(text="", selected_visible_score=1, selected_path=selected, patch_path=patch,
        changes_path=changes, trace_path=None, report={}, factory=factory)
    target = source / "answer.txt" if mutation == "source" else selected / "answer.txt" if mutation == "candidate" else changes
    target.write_text("changed")
    with pytest.raises(ValueError, match="changed"):
        result.apply_to(source)
    assert (source / "answer.txt").read_text() == ("changed" if mutation == "source" else "0")


@pytest.mark.parametrize(("before", "after"), [(b"0", b"42"), (b"0\n", b"42"), (b"0", b"42\n"),
    (b"0\n", b"42\n"), (b"0\r\n", b"42\r\n")])
def test_review_patch_applies_exact_bytes_with_or_without_final_newlines(tmp_path, before, after):
    import shutil
    import subprocess
    from my_coding_agent.rsi_branch import LocalBranchFactory
    if shutil.which("git") is None:
        pytest.skip("git required to validate standard review patch")
    source, candidate = tmp_path / "source", tmp_path / "candidate"
    source.mkdir()
    candidate.mkdir()
    (source / "answer.txt").write_bytes(before)
    (candidate / "answer.txt").write_bytes(after)
    factory = LocalBranchFactory(source, tmp_path / "out")
    _, patch, _ = factory.export(candidate)
    # Windows host autocrlf configuration is a checkout policy, not patch bytes.
    subprocess.run(["git", "-c", "core.autocrlf=false", "apply", "--check", str(patch)], cwd=source, check=True, capture_output=True)
    subprocess.run(["git", "-c", "core.autocrlf=false", "apply", str(patch)], cwd=source, check=True, capture_output=True)
    assert (source / "answer.txt").read_bytes() == after


@pytest.mark.parametrize("entry", ["inventory", "copy", "source-root", "output-parent", "copy-target", "apply-target", "apply-candidate"])
def test_windows_junctions_are_rejected_before_following_root_or_children(tmp_path, entry):
    import os
    import subprocess
    from my_coding_agent.rsi_branch import LocalBranchFactory, copy_tree, inventory
    from my_coding_agent.rsi_runtime import RsiResult
    if os.name != "nt":
        pytest.skip("Windows junction boundary")
    source, outside = tmp_path / "source", tmp_path / "outside"
    source.mkdir()
    outside.mkdir()
    (outside / "sentinel.txt").write_text("outside")
    link = source / "linked" if entry in ("inventory", "copy") else tmp_path / "junction"
    if entry.startswith("apply-"):
        factory = LocalBranchFactory(source, tmp_path / "out")
        selected, patch, changes = factory.export(outside)
        result = RsiResult(text="", selected_visible_score=1, selected_path=selected, patch_path=patch,
            changes_path=changes, trace_path=None, report={}, factory=factory)
        link = source if entry == "apply-target" else selected
        # All paths are explicit children of this synthetic tmp_path fixture.
        assert link.resolve().is_relative_to(tmp_path.resolve())
        link.rename(link.with_name(link.name + "-original"))
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
    with pytest.raises(ValueError, match="reparse|junction|symlink"):
        if entry == "inventory":
            inventory(source)
        elif entry == "copy":
            copy_tree(source, tmp_path / "copy")
        elif entry == "source-root":
            LocalBranchFactory(link, tmp_path / "out")
        elif entry == "output-parent":
            LocalBranchFactory(source, link / "new-output")
        elif entry == "copy-target":
            copy_tree(source, link / "new-copy")
        else:
            result.apply_to(source)


@pytest.mark.parametrize("path", ["C:/outside", "C:outside", "//server/share/file", "/root", "../outside", "a/../outside", "a\\outside"])
def test_inventory_relative_paths_reject_drives_unc_and_traversal(path):
    from my_coding_agent.rsi_branch import safe_relative
    with pytest.raises(ValueError):
        safe_relative(path)


def test_cleanup_failure_is_not_eligible_for_optimizer_trace(tmp_path):
    import sys
    from my_coding_agent.rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
    from my_coding_agent.rsi_runtime import FrozenPolicy, RsiRuntime
    source = tmp_path / "source"
    source.mkdir()
    factory = LocalBranchFactory(source, tmp_path / "out")
    original_close = factory.close
    async def failed_cleanup():
        await original_close()
        return [{"branch": 0, "error_type": "SyntheticCleanupFailure"}]
    factory.close = failed_cleanup
    runtime = RsiRuntime(factory=factory, evaluator=VisibleCommandEvaluator((sys.executable, "-c", "pass")),
        budget=ParentBudget(max_requests=1, max_tokens=100000, max_seconds=30, max_evaluations=2),
        policy=FrozenPolicy({"max_width": 1, "max_refine": 0}), task_id="cleanup", split="train")
    result = asyncio.run(runtime.run("fixture", llm=ScriptedLLM()))
    assert result.report["valid_attempts"] == 1 and result.report["cleanup_errors"]
    assert result.report["status"] == "cleanup_failed" and result.trace_path is None
    assert result.final_success is None and result.selected_path.is_dir()


def test_cancellation_during_owned_cleanup_still_propagates(tmp_path):
    import json
    import sys
    from my_coding_agent.rsi_branch import LocalBranchFactory, VisibleCommandEvaluator
    from my_coding_agent.rsi_runtime import FrozenPolicy, RsiRuntime
    async def exercise():
        entered = asyncio.Event()
        source = tmp_path / "source"
        source.mkdir()
        factory = LocalBranchFactory(source, tmp_path / "out")
        original_close = factory.close
        async def interrupted_close():
            await original_close()
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The real factory records child cleanup failures, including
                # cancellation, so the controller must preserve caller intent.
                return [{"branch": 0, "error_type": "CancelledError"}]
        factory.close = interrupted_close
        runtime = RsiRuntime(factory=factory, evaluator=VisibleCommandEvaluator((sys.executable, "-c", "pass")),
            budget=ParentBudget(max_requests=1, max_tokens=100000, max_seconds=30, max_evaluations=2),
            policy=FrozenPolicy({"max_width": 1, "max_refine": 0}), task_id="cleanup-cancel", split="train")
        task = asyncio.create_task(runtime.run("fixture", llm=ScriptedLLM()))
        await asyncio.wait_for(entered.wait(), 10)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        report = json.loads((factory.output / "result.json").read_text())
        assert report["status"] == "cancelled" and report["trace_sha256"] is None
        assert runtime._task is None
    asyncio.run(exercise())
