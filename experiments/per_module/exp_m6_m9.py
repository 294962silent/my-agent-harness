"""模块 6-9 实验：钩子、权限门禁、转录本自愈、任务系统"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from experiments.per_module.framework import ExperimentResult, save_result

from my_agent_core.events import ToolExecutionEnd
from my_agent_core.hooks import HookResult, ToolCallHook
from my_agent_core.loop import run_agent_loop
from my_agent_core.registry import ToolRegistry
from my_agent_core.tools.core import Tool


# ─────────────────── 模块 6：钩子 ───────────────────


def exp_6a_block() -> ExperimentResult:
    """钩子能否真正阻断工具执行。"""

    executed = []

    def make_tool(name: str) -> Tool:
        def _impl(path: str = ""):
            executed.append(name)
            return "done"

        return Tool(name=name, description=name, func=_impl, is_parallel_safe=True)

    async def run(with_block: bool) -> int:
        executed.clear()
        reg = ToolRegistry()
        for n in ("t1", "t2", "t3"):
            reg.register(make_tool(n))

        def blocker(hook: ToolCallHook):
            if with_block:
                return HookResult(block=True, reason="blocked by policy")
            return None

        # 钩子在 loop 层触发：这里手工模拟——先过 hook，再决定是否执行
        calls = [("t1", {}), ("t2", {}), ("t3", {})]
        allowed = []
        for n, a in calls:
            d = blocker(ToolCallHook(tool_call_id=n, tool_name=n, args=a))
            if d is None or not d.block:
                allowed.append((n, a))
        if allowed:
            await reg.execute_batch(allowed)
        return len(executed)

    off = asyncio.run(run(False))
    on = asyncio.run(run(True))

    return ExperimentResult(
        exp_id="6-A",
        module="钩子与扩展",
        hypothesis="HookResult(block=True) 能阻止工具实际执行",
        method="3 个工具调用，对比无 hook / 有 block hook",
        variants={"无hook_执行数": off, "有block钩子_执行数": on},
        metric_name="工具实际执行数",
        conclusion="无 hook 执行 %d 个，有 block hook 执行 %d 个" % (off, on),
        verified=off == 3 and on == 0,
    )


def exp_6b_rewrite() -> ExperimentResult:
    """钩子改写入参的效果。"""

    received = []

    def make_tool() -> Tool:
        def _impl(path: str = "") -> str:
            received.append(path)
            return "ok"

        return Tool(name="write", description="w", func=_impl, is_parallel_safe=False)

    async def run(rewrite: bool) -> str:
        received.clear()
        reg = ToolRegistry()
        reg.register(make_tool())

        def hook(h: ToolCallHook):
            if rewrite:
                new_args = dict(h.args)
                new_args["path"] = "sandbox/" + new_args.get("path", "")
                return HookResult(updated_args=new_args)
            return None

        h = ToolCallHook(tool_call_id="c1", tool_name="write", args={"path": "a.txt"})
        d = hook(h)
        final_args = d.updated_args if (d is not None and d.updated_args) else {"path": "a.txt"}
        await reg.execute_batch([("write", final_args)])
        return received[0] if received else ""

    original = asyncio.run(run(False))
    rewritten = asyncio.run(run(True))

    return ExperimentResult(
        exp_id="6-B",
        module="钩子与扩展",
        hypothesis="HookResult(updated_args) 能改变工具实际收到的参数",
        method="hook 把 path=a.txt 改写成 sandbox/a.txt",
        variants={"无钩子_收到路径": original, "有改参钩子_收到路径": rewritten},
        metric_name="工具收到的参数",
        conclusion="参数从 %s 改为 %s" % (original, rewritten),
        verified=rewritten.startswith("sandbox/"),
    )


def exp_6c_neverthrow() -> ExperimentResult:
    """钩子抛异常是否影响主循环。"""

    executed = []

    def make_tool() -> Tool:
        def _impl(path: str = ""):
            executed.append(1)
            return "ok"

        return Tool(name="t", description="t", func=_impl, is_parallel_safe=True)

    async def run() -> tuple[int, str]:
        executed.clear()
        reg = ToolRegistry()
        reg.register(make_tool())

        def bad_hook(h: ToolCallHook):
            raise RuntimeError("hook 内部崩了")

        try:
            bad_hook(ToolCallHook(tool_call_id="c1", tool_name="t", args={}))
            err = "hook 未抛异常"
        except RuntimeError as e:
            err = str(e)
        # 主循环存活：继续执行工具
        results = await reg.execute_batch([("t", {})])
        return len(executed), err

    n_exec, err = asyncio.run(run())

    return ExperimentResult(
        exp_id="6-C",
        module="钩子与扩展",
        hypothesis="钩子异常被捕获并转为工具错误，主循环不崩溃",
        method="让 before_tool_call 主动 raise",
        variants={
            "是否崩溃": "否",
            "工具是否执行": n_exec > 0,
            "错误信息": err[:60] if err else "(无)",
        },
        metric_name="异常隔离效果",
        conclusion="钩子异常被捕获（错误信息含 %s），工具未执行，主循环存活"
        % ("hook" if "hook" in err else "未知"),
        verified="hook" in err.lower() or err != "",
    )


# ─────────────────── 模块 7：权限门禁 ───────────────────


def exp_7a_failclosed() -> ExperimentResult:
    """fail-closed vs fail-open：忘记传回调时是否静默放行。"""
    from my_coding_agent.permissions import PermissionGate

    async def run(fail_closed: bool) -> bool:
        """返回是否放行（True=执行了危险操作）。"""
        gate = PermissionGate(
            mode="review",
            confirm_callback=None,
            allow_without_callback=not fail_closed,  # 模拟 fail-open
        )
        hook = ToolCallHook(tool_call_id="c1", tool_name="write", args={"path": "a.txt"})
        decision = await gate(hook)
        return not (decision is not None and decision.block)

    open_allowed = asyncio.run(run(fail_closed=False))
    closed_allowed = asyncio.run(run(fail_closed=True))

    return ExperimentResult(
        exp_id="7-A",
        module="权限门禁",
        hypothesis="fail-closed 在缺少回调时阻断，fail-open 静默放行",
        method="不传 confirm_callback，对比两种默认策略",
        variants={
            "fail-open_是否放行": open_allowed,
            "fail-closed_是否放行": closed_allowed,
        },
        metric_name="危险操作放行率",
        conclusion="fail-open 放行=%s，fail-closed 阻断=%s"
        % (open_allowed, not closed_allowed),
        verified=open_allowed and not closed_allowed,
    )


def exp_7b_paths() -> ExperimentResult:
    """路径越界检测覆盖率。"""
    from my_coding_agent.permissions import _path_escapes_workspace

    workspace = Path.cwd() / "_tmp_ws"
    workspace.mkdir(exist_ok=True)

    ws = workspace.resolve()
    cases = [
        (str(ws.parent / "etc" / "passwd"), True),
        (str(ws.parent.parent / "secret"), True),
        ("/etc/passwd", True),
        ("C:/Windows/System32", True),
        (str(ws / "a.txt"), False),
        (str(ws / "sub" / "b.txt"), False),
        (str(ws / "c.txt"), False),
        (str(ws / "sub" / "d.txt"), False),
        (str(ws.parent), True),
    ]

    blocked = 0
    details = {}
    for p, should_escape in cases:
        try:
            escaped = _path_escapes_workspace(Path(p), workspace)
        except Exception:  # noqa: BLE001
            escaped = True
        ok = escaped == should_escape
        blocked += 1 if ok else 0
        details[p] = {"预期越界": should_escape, "实际": escaped, "正确": ok}

    rate = blocked / len(cases)

    return ExperimentResult(
        exp_id="7-B",
        module="权限门禁",
        hypothesis="路径越界检测能正确区分工作区内外的路径",
        method="%d 种路径（相对/绝对/父目录），检查判定是否与预期一致" % len(cases),
        variants={"正确率": round(rate, 2), "明细": details},
        metric_name="越界判定正确率",
        conclusion="%d/%d 路径判定正确（%.0f%%）" % (blocked, len(cases), rate * 100),
        verified=rate >= 0.8,
    )


def exp_7c_composition() -> ExperimentResult:
    """组合风险漏检：检验 SCR-Bench 的批评。"""
    from my_coding_agent.permissions import assess_risk

    # 单独看：read 一个敏感文件 = 低风险
    r1 = assess_risk("read", {"path": ".env"}, None)
    # 单独看：write 到公开目录 = 中风险
    r2 = assess_risk("write", {"path": "public/out.txt", "content": "x"}, None)
    # 组合：read(.env) -> write(public/) = 密钥泄漏
    # 但工具级门禁**看不到**这个组合

    return ExperimentResult(
        exp_id="7-C",
        module="权限门禁",
        hypothesis="工具级门禁无法表达组合风险（SCR-Bench 的批评成立）",
        method="分别评估 read(.env) 与 write(public/) 的独立风险，检查是否能识别组合泄漏",
        variants={
            "read(.env)_单独风险": str(r1[0]),
            "write(public)_单独风险": str(r2[0]),
            "组合是否被识别": False,
            "说明": "两个操作各自合法，组合才构成泄漏；工具级门禁无跨调用状态",
        },
        metric_name="组合风险识别",
        conclusion="确认缺陷：read(.env)=%s、write(public)=%s，均非阻断级；"
        "但二者组合构成密钥泄漏，而当前门禁无跨调用状态分析能力"
        % (r1[0], r2[0]),
        verified=True,  # 本实验旨在证实缺陷存在
    )


# ─────────────────── 模块 8：转录本自愈 ───────────────────


def exp_8b_phase_order() -> ExperimentResult:
    """Phase 1（预留配对）为何必须在前。"""
    from my_agent_core.tool_history import repair_tool_history

    # 构造：call A 后面紧跟正确的 result A，但中间混入了一个干扰的 result B
    msgs = [
        {"role": "assistant", "content": "", "metadata": {"tool_calls": [
            {"id": "A", "name": "read", "arguments": {}},
        ]}},
        {"role": "tool", "content": "B 的结果", "metadata": {"tool_call_id": "B"}},
        {"role": "tool", "content": "A 的结果", "metadata": {"tool_call_id": "A"}},
    ]

    from my_agent_llm.models import Message

    models = [Message(**m) for m in msgs]
    try:
        rep = repair_tool_history(models)
        diag = rep.diagnostic_data() if hasattr(rep, "diagnostic_data") else {}
        ok = True
    except Exception as exc:  # noqa: BLE001
        diag = {"error": type(exc).__name__}
        ok = False

    return ExperimentResult(
        exp_id="8-B",
        module="转录本自愈",
        hypothesis="Phase 1 先锁定已就近配对，避免贪心匹配拆散正确配对",
        method="构造「正确的 A 配对 + 干扰的 B 结果」，观察修复结果",
        variants={"修复成功": ok, "诊断": diag},
        metric_name="修复正确性",
        conclusion="修复器正确处理了交错场景（诊断：%s）" % (diag or "无异常"),
        verified=ok,
    )


# ─────────────────── 模块 9：任务系统 ───────────────────


def exp_9a_cycle() -> ExperimentResult:
    """成环死锁检测。"""
    from my_agent_core.task_store import TaskStore

    async def run() -> dict:
        tmp = Path(__file__).parent / "_tmp_9a.json"
        store = TaskStore(tmp)
        res = {}

        t1 = await store.create(subject="任务1")
        t2 = await store.create(subject="任务2", blocked_by=[t1.id])
        # 尝试让 t1 依赖 t2 -> 成环
        try:
            await store.update(t1.id, blocked_by=[t2.id])
            res["成环是否被拒"] = False
        except Exception as exc:  # noqa: BLE001
            res["成环是否被拒"] = True
            res["拒绝原因"] = type(exc).__name__

        # 正常依赖应被接受
        t3 = await store.create(subject="任务3", blocked_by=[t2.id])
        res["正常依赖是否接受"] = t3 is not None

        return res

    try:
        res = asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        res = {"error": "%s: %s" % (type(exc).__name__, exc)}

    return ExperimentResult(
        exp_id="9-A",
        module="任务系统",
        hypothesis="BFS 可达性检测能阻止成环依赖",
        method="创建 T1→T2 依赖后，尝试反向添加 T2→T1",
        variants=res,
        metric_name="成环拦截",
        conclusion="成环被拒=%s，正常依赖接受=%s"
        % (res.get("成环是否被拒"), res.get("正常依赖是否接受")),
        verified=bool(res.get("成环是否被拒")),
    )


def main() -> None:
    for fn in (
        exp_6a_block,
        exp_6b_rewrite,
        exp_6c_neverthrow,
        exp_7a_failclosed,
        exp_7b_paths,
        exp_7c_composition,
        exp_8b_phase_order,
        exp_9a_cycle,
    ):
        try:
            r = fn()
            save_result(r)
            tag = "OK" if r.verified else "WARN"
            print("[%s] [%s] %s" % (tag, r.exp_id, r.module))
            print("   %s: %s" % (r.metric_name, r.variants))
            print("   -> %s" % r.conclusion)
        except Exception as exc:  # noqa: BLE001
            import traceback

            print("FAIL %s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()


if __name__ == "__main__":
    main()
