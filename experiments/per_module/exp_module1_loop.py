"""模块 1 实验：ReAct 微内核（loop.py）

1-A 纯函数 vs 类封装：装配与执行耗时、峰值内存
1-B 自动续写（截断自愈）：截断后能否挽回工具调用
1-C Steering / Follow-up 两级干预：注入时机与语义
"""

from __future__ import annotations

import asyncio
import gc
import time
import tracemalloc
from pathlib import Path
from typing import Any

from experiments.per_module.fake_llm import InstantLLM, ScriptedLLM
from experiments.per_module.framework import ExperimentResult, run_sync

from my_agent_core.events import ToolExecutionEnd
from my_agent_core.loop import run_agent_loop
from my_agent_llm.models import Message, ToolCall


# ─────────────────────────── 实验 1-A ───────────────────────────


async def _run_pure_once() -> None:
    llm = InstantLLM("done")
    async for _ in run_agent_loop(llm=llm, messages=[]):
        pass


def exp_1a_purity() -> ExperimentResult:
    """纯函数 vs 类封装：装配+执行耗时与峰值内存。"""

    # ── 实验组：纯函数（无任何装配）──
    async def measure_pure() -> tuple[float, int]:
        gc.collect()
        tracemalloc.start()
        t0 = time.perf_counter()
        await _run_pure_once()
        elapsed = time.perf_counter() - t0
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return elapsed, peak

    pure_times: list[float] = []
    pure_mems: list[int] = []
    for _ in range(20):
        t, m = asyncio.run(measure_pure())
        pure_times.append(t)
        pure_mems.append(m)

    # ── 对照组：构造完整 Agent（Session + Memory + Skills + Plugins）──
    tmp = Path(__file__).parent / "_tmp_1a"
    tmp.mkdir(exist_ok=True)

    def measure_full() -> tuple[float, int]:
        from my_agent_core.agent import Agent
        from my_agent_core.session.session import Session

        gc.collect()
        tracemalloc.start()
        t0 = time.perf_counter()
        sess = Session(path=tmp / f"s{time.perf_counter_ns()}.jsonl")
        Agent(
            llm=InstantLLM("done"),
            tools=[],
            session=sess,
            skill_dirs=[],
            subagent_dirs=[],
            extension_dirs=[],
            plugin_dirs=[],
            memory_dir=tmp / "mem",
        )
        elapsed = time.perf_counter() - t0
        _cur, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return elapsed, peak

    full_times: list[float] = []
    full_mems: list[int] = []
    full_error = ""
    for _ in range(10):
        try:
            t, m = measure_full()
            full_times.append(t)
            full_mems.append(m)
        except Exception as exc:  # noqa: BLE001
            full_error = f"{type(exc).__name__}: {exc}"
            break

    pure_avg = sum(pure_times) / len(pure_times) * 1000
    pure_mem = sum(pure_mems) / len(pure_mems) / 1024

    if full_times:
        full_avg = sum(full_times) / len(full_times) * 1000
        full_mem = sum(full_mems) / len(full_mems) / 1024
        ratio = full_avg / pure_avg if pure_avg else 0
        conclusion = (
            f"纯函数快 {ratio:.1f}×（{pure_avg:.3f}ms vs {full_avg:.3f}ms），"
            f"峰值内存省 {(1 - pure_mem / full_mem) * 100:.0f}%"
        )
        variants = {
            "纯函数_耗时_ms": round(pure_avg, 4),
            "类封装_耗时_ms": round(full_avg, 3),
            "加速倍数": round(ratio, 1),
            "纯函数_峰值内存_KB": round(pure_mem, 1),
            "类封装_峰值内存_KB": round(full_mem, 1),
            "内存节省_%": round((1 - pure_mem / full_mem) * 100, 1),
        }
        verified = ratio > 1
    else:
        conclusion = f"类封装构造失败：{full_error}"
        variants = {
            "纯函数_耗时_ms": round(pure_avg, 4),
            "纯函数_峰值内存_KB": round(pure_mem, 1),
            "类封装": full_error,
        }
        verified = False

    return ExperimentResult(
        exp_id="1-A",
        module="ReAct 微内核",
        hypothesis="纯函数生成器无需装配整个 Agent，单测更快、内存更省",
        method="纯 run_agent_loop(InstantLLM, []) ×20 vs 完整 Agent(...) 构造 ×10",
        variants=variants,
        metric_name="装配+执行耗时 / 峰值内存",
        conclusion=conclusion,
        verified=verified,
    )


# ─────────────────────────── 实验 1-B ───────────────────────────


def exp_1b_continuation() -> ExperimentResult:
    """自动续写：截断时能否挽回工具调用。

    注意：MAX_LENGTH_CONTINUATIONS 是 run_agent_loop 内的局部常量（loop.py:585），
    无法从外部关闭。因此本实验改为「对照模型调用次数」：
    - 场景A：第1次直接返回 tool_calls（无截断）→ 基线
    - 场景B：第1次 length 无工具，第2次 tool_calls → 需续写才能成功
    若 B 的工具执行率与 A 相同，说明续写机制生效。
    """

    write_call = ToolCall(id="c1", name="write", args={"path": "a.txt", "content": "hi"})

    async def run_script(script) -> tuple[bool, int, int]:
        llm = ScriptedLLM(script)
        tool_ran = False
        n_events = 0
        async for ev in run_agent_loop(llm=llm, messages=[]):
            n_events += 1
            if isinstance(ev, ToolExecutionEnd):
                tool_ran = True
            if n_events > 200:
                break
        return tool_ran, llm.calls, n_events

    # A：无截断基线
    a_ok, a_calls, a_ev = asyncio.run(
        run_script([("going to write", "tool_calls", [write_call])])
    )
    # B：先截断，再给工具
    b_ok, b_calls, b_ev = asyncio.run(
        run_script([
            ("思考到一半被截断……", "length"),
            ("", "tool_calls", [write_call]),
        ])
    )

    return ExperimentResult(
        exp_id="1-B",
        module="ReAct 微内核",
        hypothesis="截断后自动续写能让模型补出完整的 tool_call，效果等同无截断",
        method="场景A(无截断) vs 场景B(先 length 再给工具)，比较工具执行与模型调用次数",
        variants={
            "A无截断_工具执行": a_ok,
            "A无截断_模型调用": a_calls,
            "B先截断_工具执行": b_ok,
            "B先截断_模型调用": b_calls,
            "续写额外调用": b_calls - a_calls,
        },
        metric_name="截断场景下工具执行率",
        conclusion=(
            f"截断场景仍拿到工具（额外 {b_calls - a_calls} 次模型调用），续写生效"
            if b_ok and a_ok
            else (
                "截断场景失败（无续写）"
                if (a_ok and not b_ok)
                else "两场景都失败，需检查假 LLM 协议"
            )
        ),
        verified=b_ok and a_ok,
    )


# ─────────────────────────── 实验 1-C ───────────────────────────


def exp_1c_intervention() -> ExperimentResult:
    """Steering / Follow-up 注入时机。"""

    async def run_scenario(
        *, steer_times: int, fup_times: int, llm_script_len: int
    ) -> dict[str, int]:
        """steer_times/fup_times：返回非空消息的次数（0=从不）。"""
        steer_n = {"v": 0}
        fup_n = {"v": 0}

        def get_steer():
            steer_n["v"] += 1
            if steer_times and steer_n["v"] <= steer_times:
                return [Message(role="user", content="steering")]
            return []

        def get_fup():
            fup_n["v"] += 1
            if fup_times and fup_n["v"] <= fup_times:
                return [Message(role="user", content="followup")]
            return []

        llm = ScriptedLLM([("ok", "end_turn")] * llm_script_len)
        counts = {"TurnEnd": 0, "AgentEnd": 0, "MessageEnd": 0, "total": 0}
        async for ev in run_agent_loop(
            llm=llm,
            messages=[],
            get_steering_messages=get_steer,
            get_follow_up_messages=get_fup,
        ):
            counts["total"] += 1
            name = type(ev).__name__
            if name in counts:
                counts[name] += 1
            if counts["total"] > 400:
                break
        counts["llm_calls"] = llm.calls
        return counts

    # steering 只在内层循环内收割：模型必须先产生工具调用才能进入下一轮
    base = asyncio.run(run_scenario(steer_times=0, fup_times=0, llm_script_len=3))
    # 用 3 次 steering（覆盖多轮内层），确保至少有一次被收割
    only_steer = asyncio.run(run_scenario(steer_times=3, fup_times=0, llm_script_len=6))
    only_fup = asyncio.run(run_scenario(steer_times=0, fup_times=1, llm_script_len=6))
    both = asyncio.run(run_scenario(steer_times=3, fup_times=1, llm_script_len=6))

    steer_effect = only_steer["llm_calls"] - base["llm_calls"]
    fup_effect = only_fup["llm_calls"] - base["llm_calls"]

    return ExperimentResult(
        exp_id="1-C",
        module="ReAct 微内核",
        hypothesis="steering 与 follow-up 都能延长会话，但收割时机不同（内层 vs 外层）",
        method="4 种注入组合，比较模型调用次数与事件规模",
        variants={
            "基线_模型调用": base["llm_calls"],
            "仅steering_模型调用": only_steer["llm_calls"],
            "仅followup_模型调用": only_fup["llm_calls"],
            "两者_模型调用": both["llm_calls"],
            "steering增量": steer_effect,
            "followup增量": fup_effect,
        },
        metric_name="干预引起的模型调用增量",
        conclusion=(
            f"steering 使调用 +{steer_effect}，follow-up 使调用 +{fup_effect}，"
            f"两者叠加 +{both['llm_calls'] - base['llm_calls']}"
        ),
        verified=steer_effect > 0 and fup_effect > 0,
    )


def run_all() -> None:
    for fn in (exp_1a_purity, exp_1b_continuation, exp_1c_intervention):
        try:
            run_sync(fn)
        except Exception as exc:  # noqa: BLE001
            import traceback

            print(f"❌ {fn.__name__}: {type(exc).__name__}: {exc}")
            traceback.print_exc()


if __name__ == "__main__":
    run_all()
