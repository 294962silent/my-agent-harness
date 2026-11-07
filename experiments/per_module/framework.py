"""逐模块实验框架：统一的实验骨架 + 结果收集。

每个实验都是一个函数，返回 dict：
    {"name": ..., "hypothesis": ..., "variants": {...}, "metric": ...}
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

RESULTS_PATH = Path(__file__).parent / "results.json"


@dataclass
class Variant:
    """一个实验分组。"""

    label: str
    value: Any
    note: str = ""


@dataclass
class ExperimentResult:
    """一个实验的完整结果。"""

    exp_id: str
    module: str
    hypothesis: str
    method: str
    variants: dict[str, Any] = field(default_factory=dict)
    metric_name: str = ""
    conclusion: str = ""
    verified: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_results() -> list[dict[str, Any]]:
    if not RESULTS_PATH.exists():
        return []
    try:
        return json.loads(RESULTS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []


def save_result(result: ExperimentResult) -> None:
    """保存（同 exp_id 覆盖）。"""
    data = load_results()
    data = [d for d in data if d.get("exp_id") != result.exp_id]
    data.append(result.to_dict())
    data.sort(key=lambda d: d.get("exp_id", ""))
    RESULTS_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_sync(fn: Callable[[], ExperimentResult]) -> ExperimentResult:
    t0 = time.perf_counter()
    result = fn()
    elapsed = time.perf_counter() - t0
    save_result(result)
    status = "✅" if result.verified else "⚠️"
    print(f"{status} [{result.exp_id}] {result.module}")
    print(f"   {result.metric_name}: {result.variants}")
    print(f"   → {result.conclusion}  ({elapsed:.2f}s)")
    return result


def run_async(fn: Callable[[], Any]) -> ExperimentResult:
    t0 = time.perf_counter()
    result = asyncio.run(fn())
    elapsed = time.perf_counter() - t0
    save_result(result)
    status = "✅" if result.verified else "⚠️"
    print(f"{status} [{result.exp_id}] {result.module}")
    print(f"   {result.metric_name}: {result.variants}")
    print(f"   → {result.conclusion}  ({elapsed:.2f}s)")
    return result
