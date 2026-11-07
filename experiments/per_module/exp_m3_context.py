"""模块 3 实验：上下文压缩（context.py）"""

from __future__ import annotations

import asyncio
from pathlib import Path

from experiments.per_module.framework import ExperimentResult, save_result

from my_agent_core.context import ContextManager, estimate_tokens
from my_agent_llm.events import StreamDoneEvent, StreamStartEvent
from my_agent_llm.models import Message

NL = chr(10)
SUMMARY_TEXT = "<summary>" + NL + "## Goal" + NL + "继续任务" + NL + "</summary>"


def _text(m: Message) -> str:
    c = m.content
    return c if isinstance(c, str) else str(c)


def common_prefix_len(a, b) -> int:
    n = 0
    for x, y in zip(a, b):
        if x.role != y.role or _text(x) != _text(y):
            break
        n += 1
    return n


def _prefix_changes(views) -> int:
    changes = 0
    for i in range(1, len(views)):
        prev, cur = views[i - 1], views[i]
        if common_prefix_len(prev, cur) < len(prev):
            changes += 1
    return changes


def _retention(views) -> float:
    if len(views) < 2:
        return 0.0
    vals = []
    for i in range(1, len(views)):
        prev, cur = views[i - 1], views[i]
        vals.append(common_prefix_len(prev, cur) / max(1, len(prev)))
    return sum(vals) / len(vals) * 100


def build_turns(n_turns: int, big_turns: set, big_size: int = 8000):
    msgs = []
    for i in range(n_turns):
        msgs.append(Message(role="user", content="第 %d 轮：处理任务 %d" % (i, i)))
        msgs.append(
            Message(
                role="assistant",
                content="处理任务 %d" % i,
                metadata={"stop_reason": "tool_calls"},
            )
        )
        body = ("X" * big_size) if i in big_turns else ("结果 %d" % i)
        msgs.append(
            Message(role="tool", content=body, metadata={"tool_call_id": "c%d" % i})
        )
    return msgs


class CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def astream_events(self, **kwargs):
        self.calls += 1
        yield StreamStartEvent(partial=Message(role="assistant", content=""))
        yield StreamDoneEvent(
            message=Message(
                role="assistant",
                content=SUMMARY_TEXT,
                metadata={"stop_reason": "end_turn"},
            )
        )


class SlidingWindow:
    def __init__(self, keep: int = 12) -> None:
        self.keep = keep

    def view(self, messages, step):
        return messages[-self.keep :]


class Retruncate:
    def __init__(self, base: int = 500) -> None:
        self.base = base

    def view(self, messages, step):
        limit = max(50, self.base - step * 20)
        out = []
        for m in messages:
            if m.role == "tool" and len(_text(m)) > limit:
                out.append(
                    Message(role="tool", content=_text(m)[:limit], metadata=m.metadata)
                )
            else:
                out.append(m)
        return out


class ThresholdSummary:
    def __init__(self, budget: int = 4000) -> None:
        self.budget = budget

    def view(self, messages, step):
        if estimate_tokens(messages) <= self.budget:
            return list(messages)
        cut = len(messages) // 2
        return [Message(role="user", content="[摘要] 前文已压缩")] + messages[cut:]


async def exp_3a() -> ExperimentResult:
    N = 20
    all_msgs = build_turns(N, {5, 10, 15})

    def collect(s):
        return [s.view(all_msgs[: i * 3], i) for i in range(1, N + 1)]

    va = collect(SlidingWindow(12))
    vb = collect(Retruncate(500))
    vc = collect(ThresholdSummary(4000))

    tmp = Path(__file__).parent / "_tmp_3a"
    tmp.mkdir(exist_ok=True)
    cm = ContextManager(
        budget=4000, llm=CountingLLM(), keep_recent_tokens=1000, results_dir=tmp
    )
    vo = [await cm.prepare(all_msgs[: i * 3]) for i in range(1, N + 1)]

    changes = {
        "A滑动窗口": _prefix_changes(va),
        "B动态重截断": _prefix_changes(vb),
        "C阈值摘要": _prefix_changes(vc),
        "本项目三层": _prefix_changes(vo),
    }
    ret = {
        "A滑动窗口": round(_retention(va), 1),
        "B动态重截断": round(_retention(vb), 1),
        "C阈值摘要": round(_retention(vc), 1),
        "本项目三层": round(_retention(vo), 1),
    }
    best_other = min(changes["A滑动窗口"], changes["B动态重截断"], changes["C阈值摘要"])
    return ExperimentResult(
        exp_id="3-A",
        module="上下文压缩",
        hypothesis="三层管线（Append-Only 优先）显著减少 prefix 变更，保护 KV cache",
        method="%d 轮对话（含 3 轮超大工具输出），对比 4 种策略" % N,
        variants={"prefix变更次数": changes, "平均prefix保留率_%": ret},
        metric_name="prefix 变更次数 / 保留率",
        conclusion="本项目 %d 次变更、保留率 %s%%；最优对照 %d 次"
        % (changes["本项目三层"], ret["本项目三层"], best_other),
        verified=changes["本项目三层"] <= best_other,
    )


async def exp_3b() -> ExperimentResult:
    msgs = []
    for i in range(10):
        msgs.append(Message(role="user", content="任务 %d" % i))
        msgs.append(Message(role="assistant", content="处理 %d" % i))
    for i in range(3):
        msgs.append(
            Message(
                role="tool", content="X" * 25000, metadata={"tool_call_id": "c%d" % i}
            )
        )

    raw = estimate_tokens(msgs)
    tmp = Path(__file__).parent / "_tmp_3b"
    tmp.mkdir(exist_ok=True)

    l1 = CountingLLM()
    cm1 = ContextManager(budget=4000, llm=l1, keep_recent_tokens=800, results_dir=tmp)
    t_with = estimate_tokens(await cm1.prepare(msgs))

    l2 = CountingLLM()
    cm2 = ContextManager(budget=4000, llm=l2, keep_recent_tokens=800, results_dir=None)
    t_without = estimate_tokens(await cm2.prepare(msgs))

    spilled = list(tmp.glob("*.txt"))
    saved = raw - t_with
    return ExperimentResult(
        exp_id="3-B",
        module="上下文压缩",
        hypothesis="免费层零 LLM 调用即可把上下文压回阈值内",
        method="同一份上下文（3 条 25000 字符工具输出，超落盘阈值 20000）",
        variants={
            "原始token": raw,
            "有免费层_摘要调用": l1.calls,
            "无免费层_摘要调用": l2.calls,
            "有免费层_最终token": t_with,
            "无免费层_最终token": t_without,
            "节省token": saved,
            "节省比例_%": round(saved / raw * 100, 1) if raw else 0,
            "落盘文件数": len(spilled),
        },
        metric_name="摘要调用次数 / token 节省",
        conclusion="%d 到 %d token（省 %.0f%%），落盘 %d 个文件"
        % (raw, t_with, (saved / raw * 100) if raw else 0, len(spilled)),
        verified=l1.calls < l2.calls or t_with < t_without,
    )


async def exp_3c() -> ExperimentResult:
    tmp = Path(__file__).parent / "_tmp_3c"
    tmp.mkdir(exist_ok=True)
    all_msgs = build_turns(12, {4, 8})
    res = {}

    cm = ContextManager(
        budget=3000, llm=CountingLLM(), keep_recent_tokens=800, results_dir=tmp
    )
    await cm.prepare(all_msgs)
    tampered = all_msgs[:6] + all_msgs[7:]
    try:
        v = await cm.prepare(tampered)
        res["删除中间消息_成功"] = True
        res["删除后视图长度"] = len(v)
    except Exception as e:  # noqa: BLE001
        res["删除中间消息_成功"] = False
        res["删除后错误"] = type(e).__name__

    cm2 = ContextManager(
        budget=3000, llm=CountingLLM(), keep_recent_tokens=800, results_dir=tmp
    )
    await cm2.prepare(all_msgs)
    rep = list(all_msgs)
    rep[5] = Message(role="tool", content="被替换", metadata={"tool_call_id": "c1"})
    try:
        v2 = await cm2.prepare(rep)
        res["替换消息_成功"] = True
        res["替换后视图长度"] = len(v2)
    except Exception as e:  # noqa: BLE001
        res["替换消息_成功"] = False
        res["替换后错误"] = type(e).__name__

    res["缓存检测失效"] = cm._summary is None or cm._locate_boundary(tampered) is None
    ok = bool(res.get("删除中间消息_成功")) and bool(res.get("替换消息_成功"))
    return ExperimentResult(
        exp_id="3-C",
        module="上下文压缩",
        hypothesis="历史被篡改时，key 定位能检测边界失效并安全重建",
        method="压缩建立缓存后，分别删除/替换中间消息",
        variants=res,
        metric_name="篡改后的处理结果",
        conclusion="删除与替换均安全处理" if ok else "篡改导致异常",
        verified=ok,
    )


async def main() -> None:
    for coro in (exp_3a(), exp_3b(), exp_3c()):
        try:
            r = await coro
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
    asyncio.run(main())
