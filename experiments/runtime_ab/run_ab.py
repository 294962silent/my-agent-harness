"""运行时自愈的 A/B 实验。

对照组（v0）：关闭转录本自愈（``clean_provider_context`` 退化为直通）。
实验组（v1）：完整运行时。

指标：**会话可继续率** —— 在「脏转录本 + 严格 provider」的前提下，
下一次请求能否被 provider 接受并正常结束。这是真实场景里用户看到的现象：
崩溃/打断之后再发一句话，Agent 直接报 400 或者干脆沉默。

用法：
    uv run python experiments/runtime_ab/run_ab.py
    uv run python experiments/runtime_ab/run_ab.py --per-family 12 --json out.json
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# 允许直接 `uv run python experiments/runtime_ab/run_ab.py`（不依赖 PYTHONPATH）
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# 隔离用户目录，避免污染真实的 ~/.my-pi-agent
_HOME = Path(tempfile.mkdtemp(prefix="runtime-ab-home-"))
os.environ["MY_AGENT_HOME"] = str(_HOME)
os.environ.setdefault("PYTHONIOENCODING", "utf-8")

from my_agent_core import agent as core_agent_module  # noqa: E402
from my_agent_core import loop as core_loop_module  # noqa: E402
from my_agent_core.events import AgentEnd  # noqa: E402
from my_agent_core.retry import AutoRetryPolicy  # noqa: E402
from my_agent_core.session import Session  # noqa: E402

from experiments.runtime_ab.scenarios import Scenario, build_scenarios  # noqa: E402
from experiments.runtime_ab.strict_provider import (  # noqa: E402
    ProviderContractError,
    StrictProvider,
)
from my_coding_agent.agent import CodingAgent  # noqa: E402


@dataclass
class Result:
    scenario: str
    family: str
    arm: str
    ok: bool
    provider_rejected: bool
    stop_reason: str | None
    messages_in: int
    messages_out: int
    repair_delta: int
    error: str | None


def _identity(messages):
    return list(messages)


@contextlib.contextmanager
def _without_self_healing(enabled: bool):
    """v0 时把自愈替换成直通；v1 时保持原样。"""
    if enabled:
        yield
        return
    originals = {
        "agent": core_agent_module.clean_provider_context,
        "loop": core_loop_module.clean_provider_context,
    }
    core_agent_module.clean_provider_context = _identity  # type: ignore[assignment]
    core_loop_module.clean_provider_context = _identity  # type: ignore[assignment]
    try:
        yield
    finally:
        core_agent_module.clean_provider_context = originals["agent"]  # type: ignore[assignment]
        core_loop_module.clean_provider_context = originals["loop"]  # type: ignore[assignment]


async def run_case(scenario: Scenario, arm: str) -> Result:
    workspace = Path(tempfile.mkdtemp(prefix=f"ab-{scenario.name}-"))
    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    (workspace / "b.txt").write_text("beta\n", encoding="utf-8")

    session = Session(path=workspace / "session.jsonl", cwd=str(workspace))
    for turn in scenario.turns:
        session.add_message(turn.role, turn.content, **turn.metadata)

    history = session.get_full_history_messages()
    # 修复的「代价」：自愈会补齐/剥离多少条消息（干净历史应当为 0）
    repair_delta = len(core_agent_module.clean_provider_context(history)) - len(history)
    provider = StrictProvider()
    agent = CodingAgent(
        workspace=workspace,
        llm=provider,
        session=session,
        system_prompt="You are a coding agent.",
        auto_load_mcp=False,
        retry_policy=AutoRetryPolicy(max_retries=0),
    )

    stop_reason: str | None = None
    error: str | None = None
    with _without_self_healing(arm == "v1"):
        try:
            async for event in agent.run_stream("继续"):
                if isinstance(event, AgentEnd):
                    stop_reason = event.stop_reason
        except ProviderContractError as exc:  # provider 400：会话不可继续
            error = str(exc)
        except Exception as exc:  # noqa: BLE001 - 实验需要兜住一切
            error = f"{type(exc).__name__}: {exc}"

    rejected = bool(error and "400" in error) or provider.rejected
    ok = bool(stop_reason == "end_turn") and error is None

    return Result(
        scenario=scenario.name,
        family=scenario.family,
        arm=arm,
        ok=ok,
        provider_rejected=rejected,
        stop_reason=stop_reason,
        messages_in=len(history),
        messages_out=len(agent.agent.messages),
        repair_delta=repair_delta,
        error=error,
    )


def summarize(results: list[Result]) -> dict[str, Any]:
    families: dict[str, dict[str, Any]] = {}
    for res in results:
        fam = families.setdefault(res.family, {"v0": [0, 0], "v1": [0, 0]})
        fam[res.arm][1] += 1
        if res.ok:
            fam[res.arm][0] += 1

    def pct(pair: list[int]) -> float:
        return round(100.0 * pair[0] / pair[1], 1) if pair[1] else 0.0

    rows = [
        {
            "family": name,
            "n": counts["v0"][1],
            "v0": f"{counts['v0'][0]}/{counts['v0'][1]} ({pct(counts['v0'])}%)",
            "v1": f"{counts['v1'][0]}/{counts['v1'][1]} ({pct(counts['v1'])}%)",
            "delta": round(
                sum(r.repair_delta for r in results if r.family == name and r.arm == "v1")
                / max(1, sum(1 for r in results if r.family == name and r.arm == "v1")),
                1,
            ),
            "touched": sum(
                1 for r in results if r.family == name and r.arm == "v1" and r.repair_delta != 0
            ),
        }
        for name, counts in families.items()
    ]
    v0 = [sum(1 for r in results if r.arm == "v0" and r.ok), sum(1 for r in results if r.arm == "v0")]
    v1 = [sum(1 for r in results if r.arm == "v1" and r.ok), sum(1 for r in results if r.arm == "v1")]
    return {
        "rows": rows,
        "overall": {"v0": pct(v0), "v1": pct(v1), "v0_raw": v0, "v1_raw": v1},
    }


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--per-family", type=int, default=8)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args()

    scenarios = build_scenarios(per_family=args.per_family)
    results: list[Result] = []
    for scenario in scenarios:
        for arm in ("v0", "v1"):
            results.append(await run_case(scenario, arm))

    summary = summarize(results)

    print("\n## 运行时自愈 A/B（会话可继续率）\n")
    print("| 故障类型 | 样本 | v0 关闭自愈 | v1 完整运行时 | 平均修复动作 |")
    print("| --- | --- | --- | --- | --- |")
    for row in summary["rows"]:
        print(
            f"| {row['family']} | {row['n']} | {row['v0']} | {row['v1']} | "
            f"{row['delta']:+} 条 / {row['touched']} 例触发 |"
        )
    overall = summary["overall"]
    print(
        f"| **合计** | {overall['v0_raw'][1]} | {overall['v0_raw'][0]}/{overall['v0_raw'][1]} "
        f"({overall['v0']}%) | {overall['v1_raw'][0]}/{overall['v1_raw'][1]} ({overall['v1']}%) |"
    )

    if args.json:
        args.json.write_text(
            json.dumps(
                {"summary": summary, "results": [asdict(r) for r in results]},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\n详细结果写入 {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
