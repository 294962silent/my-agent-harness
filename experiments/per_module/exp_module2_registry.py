"""模块 2 实验：工具执行与并发（registry.py）

2-A 批级门控 vs 全并发 vs 全串行：速度与正确性
2-B is_parallel_safe 默认值的失败安全价值
2-C 流式进度回传的延迟改善
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from experiments.per_module.framework import ExperimentResult, run_async

from my_agent_core.registry import ToolRegistry
from my_agent_core.tools.core import Tool


# ───────────────────────── 公共：带竞态检测的假工具 ─────────────────────────

SHARED: dict[str, Any] = {}


def _reset() -> None:
    SHARED.clear()
    SHARED["version"] = 0
    SHARED["read_log"] = []
    SHARED["write_count"] = 0


async def _write_impl(path: str, content: str = "") -> str:
    """写工具：先等待（模拟 IO），再递增版本号。

    故意把 sleep 放在写入之前，这样并发时读工具会抢在写入前执行，
    读到旧版本 —— 这就是竞态的可观测信号。
    """
    await asyncio.sleep(0.02)
    SHARED["version"] += 1
    SHARED["write_count"] += 1
    return f"wrote {path} v{SHARED['version']}"


async def _read_impl(path: str) -> str:
    """读工具：记录读到的版本号（用于检测竞态）。"""
    await asyncio.sleep(0.01)
    v = SHARED["version"]
    SHARED["read_log"].append(v)
    return f"read {path} v{v}"


def make_write_tool(parallel_safe: bool = False) -> Tool:
    return Tool(
        name="write",
        description="write",
        func=_write_impl,
        is_parallel_safe=parallel_safe,
    )


def make_read_tool(parallel_safe: bool = True) -> Tool:
    return Tool(
        name="read",
        description="read",
        func=_read_impl,
        is_parallel_safe=parallel_safe,
    )


# ─────────────────────────── 实验 2-A ───────────────────────────


async def _run_batch(registry: ToolRegistry, calls: list[tuple[str, dict]]) -> float:
    """执行一批调用，返回耗时。"""
    t0 = time.perf_counter()
    await registry.execute_batch(calls)
    return time.perf_counter() - t0


async def exp_2a_concurrency() -> ExperimentResult:
    """批级门控：全只读并发，含写则整批串行。

    三种策略由 ToolRegistry 的 is_parallel_safe 决定，无法直接切换"全并发/全串行"，
    因此用「把工具都标为 parallel_safe / 都不标」来模拟。
    """

    results: dict[str, Any] = {}

    # ── 场景 1：5 个只读工具 ──
    _reset()
    reg_ro = ToolRegistry()
    reg_ro.register(make_read_tool(parallel_safe=True))
    t_ro = await _run_batch(reg_ro, [("read", {"path": f"f{i}"}) for i in range(5)])
    results["全只读_耗时_ms"] = round(t_ro * 1000, 1)
    results["全只读_读到版本"] = SHARED["read_log"][:]

    # ── 场景 2：1 写 + 4 读（批级门控 → 全串行）──
    _reset()
    reg_mix = ToolRegistry()
    reg_mix.register(make_read_tool(parallel_safe=True))
    reg_mix.register(make_write_tool(parallel_safe=False))
    calls_mix = [("write", {"path": "a"})] + [("read", {"path": f"f{i}"}) for i in range(4)]
    t_mix = await _run_batch(reg_mix, calls_mix)
    results["混合批次_耗时_ms"] = round(t_mix * 1000, 1)
    log_mix = SHARED["read_log"][:]
    results["混合批次_读到版本"] = log_mix
    # 正确性：所有 read 都应读到 v1（写之后），且单调
    # 正确性：所有 read 都应在 write 之后执行，即版本 >= 1
    results["混合批次_读到最新"] = all(v >= 1 for v in log_mix) and len(log_mix) == 4

    # ── 场景 3：把写也标为 parallel_safe（模拟"全并发"）──
    _reset()
    reg_all = ToolRegistry()
    reg_all.register(make_read_tool(parallel_safe=True))
    reg_all.register(make_write_tool(parallel_safe=True))
    t_all = await _run_batch(reg_all, calls_mix)
    results["强制全并发_耗时_ms"] = round(t_all * 1000, 1)
    log_all = SHARED["read_log"][:]
    results["强制全并发_读到版本"] = log_all
    results["强制全并发_读到最新"] = all(v >= 1 for v in log_all) and len(log_all) == 4

    # 结论
    speedup = t_mix / t_ro if t_ro else 0
    return ExperimentResult(
        exp_id="2-A",
        module="工具执行与并发",
        hypothesis="批级门控：全只读批次能并发（快），含写批次强制串行（正确）",
        method="三种批次组成 × 两种声明方式，比较耗时与读版本单调性",
        variants=results,
        metric_name="批次耗时 / 读版本单调性",
        conclusion=(
            f"全只读批次 {results['全只读_耗时_ms']}ms；混合批次串行 "
            f"{results['混合批次_耗时_ms']}ms（慢 {speedup:.1f}×）；"
            f"强制全并发 {results['强制全并发_耗时_ms']}ms，"
            f"读到最新={results['强制全并发_读到最新']}"
        ),
        verified=results["混合批次_读到最新"] is True and results["强制全并发_读到最新"] is False,
    )


# ─────────────────────────── 实验 2-B ───────────────────────────


async def exp_2b_failsafe() -> ExperimentResult:
    """is_parallel_safe 默认 False 的失败安全价值。

    模拟「开发者忘记标记新工具」的场景：
    - 默认 True（fail-open）：新工具会被并发执行
    - 默认 False（fail-safe）：新工具强制串行
    """

    results: dict[str, Any] = {}

    # 默认值检查
    default_tool = Tool(name="x", description="x", func=lambda: "ok")
    results["Tool默认is_parallel_safe"] = default_tool.is_parallel_safe

    # 场景：一个未声明的新工具 + 4 个只读工具
    _reset()
    reg = ToolRegistry()
    reg.register(make_read_tool(parallel_safe=True))

    async def _unknown_impl(path: str = "") -> str:
        await asyncio.sleep(0.01)
        SHARED["version"] += 1
        return "unknown-written"

    # 未声明 is_parallel_safe（用默认值）
    reg.register(Tool(name="unknown", description="u", func=_unknown_impl))

    calls = [("unknown", {"path": "a"})] + [("read", {"path": f"f{i}"}) for i in range(4)]
    t0 = time.perf_counter()
    await reg.execute_batch(calls)
    elapsed = time.perf_counter() - t0

    results["未声明工具_批次耗时_ms"] = round(elapsed * 1000, 1)
    results["未声明工具_是否串行"] = elapsed > 0.04  # 5 个 × 10ms = 50ms 串行；并发约 10ms
    results["读到版本"] = SHARED["read_log"][:]

    return ExperimentResult(
        exp_id="2-B",
        module="工具执行与并发",
        hypothesis="is_parallel_safe 默认 False 使未声明的新工具强制串行（fail-safe）",
        method="注册一个未声明 is_parallel_safe 的工具 + 4 个只读工具，观察是否被串行",
        variants=results,
        metric_name="未声明工具的批次耗时",
        conclusion=(
            f"默认值={results['Tool默认is_parallel_safe']}，"
            f"未声明工具批次 {results['未声明工具_批次耗时_ms']}ms "
            f"（串行={'是' if results['未声明工具_是否串行'] else '否'}）"
        ),
        verified=default_tool.is_parallel_safe is False,
    )


# ─────────────────────────── 实验 2-C ───────────────────────────


async def exp_2c_streaming() -> ExperimentResult:
    """流式进度回传：首字节延迟。

    模拟一个产出 10 行的命令：
    - 全缓冲：等全部完成才可见
    - 流式：第一行完成即可见
    """

    N_LINES = 10
    LINE_DELAY = 0.02

    async def _fake_long_command(on_update=None) -> str:
        lines = []
        for i in range(N_LINES):
            await asyncio.sleep(LINE_DELAY)
            lines.append(f"line{i}")
            if on_update is not None:
                on_update("\n".join(lines))
        return "\n".join(lines)

    # ── 全缓冲 ──
    t0 = time.perf_counter()
    await _fake_long_command()
    buffered_total = time.perf_counter() - t0

    # ── 流式 ──
    first_update_time = {"t": None}

    def on_update(snapshot: str) -> None:
        if first_update_time["t"] is None:
            first_update_time["t"] = time.perf_counter() - t_stream_start

    t_stream_start = time.perf_counter()
    await _fake_long_command(on_update=on_update)
    stream_total = time.perf_counter() - t_stream_start

    buffered_first = buffered_total  # 全缓冲：首字节 = 总耗时
    stream_first = first_update_time["t"] or stream_total

    return ExperimentResult(
        exp_id="2-C",
        module="工具执行与并发",
        hypothesis="流式进度回传大幅降低首次反馈延迟",
        method=f"{N_LINES} 行输出、每行 {LINE_DELAY * 1000:.0f}ms，对比全缓冲 vs 逐行回调",
        variants={
            "全缓冲_首字节_ms": round(buffered_first * 1000, 1),
            "全缓冲_总耗时_ms": round(buffered_total * 1000, 1),
            "流式_首字节_ms": round(stream_first * 1000, 1),
            "流式_总耗时_ms": round(stream_total * 1000, 1),
            "首字节改善倍数": round(buffered_first / stream_first, 1) if stream_first else 0,
        },
        metric_name="首字节延迟",
        conclusion=(
            f"首字节从 {buffered_first * 1000:.0f}ms 降到 {stream_first * 1000:.0f}ms"
            f"（改善 {buffered_first / stream_first:.1f}×）"
        ),
        verified=stream_first < buffered_first,
    )


async def _main() -> None:
    for coro in (exp_2a_concurrency(), exp_2b_failsafe(), exp_2c_streaming()):
        try:
            result = await coro
            from experiments.per_module.framework import save_result

            save_result(result)
            status = "✅" if result.verified else "⚠️"
            print(f"{status} [{result.exp_id}] {result.module}")
            print(f"   {result.metric_name}: {result.variants}")
            print(f"   → {result.conclusion}")
        except Exception as exc:  # noqa: BLE001
            import traceback

            print(f"❌ 实验失败: {type(exc).__name__}: {exc}")
            traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(_main())
