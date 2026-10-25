"""汇总 LongMemEval 各臂结果（准确率 / 分项 / token），并给出 Wilson 置信区间。

用法：uv run python experiments/longmemeval/summarize.py
"""

from __future__ import annotations

import json
import math
from pathlib import Path

RESULTS = Path(__file__).resolve().parent / "results"

ARMS = [
    ("截断@2200", "hyp_trunc_s_pt8"),
    ("纯检索", "hyp_rag_s_pt8"),
    ("画像+时间线(v2p)", "hyp_v2p_s_pt16"),
    ("事实(v2s)", "hyp_v2s_s_pt8"),
    ("完整三层(v3)", "hyp_v3_s_pt8"),
    ("全量上下文", "hyp_full_s_pt16"),
]

TYPES = [
    "knowledge-update",
    "multi-session",
    "single-session-preference",
    "temporal-reasoning",
]


def _jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def wilson(ok: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 置信区间，返回百分比。"""
    if n == 0:
        return (0.0, 0.0)
    p = ok / n
    denom = 1 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, (center - margin) * 100), min(100.0, (center + margin) * 100))


def main() -> int:
    print(
        f"{'arm':18s} {'acc':>12s} {'95%CI':>14s} {'KU':>6s} {'multi':>6s} {'pref':>6s} {'temp':>6s} {'注入tok':>9s} {'合计tok':>9s}"
    )
    for name, tag in ARMS:
        hyp = _jsonl(RESULTS / f"{tag}.jsonl")
        judged = _jsonl(RESULTS / f"{tag}.judged.jsonl")
        if not judged:
            print(f"{name:18s} (未判分: {tag})")
            continue

        by_type: dict[str, list[int]] = {}
        for item in judged:
            slot = by_type.setdefault(item["question_type"], [0, 0])
            slot[0] += int(item["correct"])
            slot[1] += 1

        ok = sum(1 for x in judged if x["correct"])
        n = len(judged)
        lo, hi = wilson(ok, n)
        prompt = [x["prompt_tokens"] for x in hyp if x.get("prompt_tokens")]
        extra = [x.get("extraction_tokens", 0) for x in hyp]
        avg_prompt = int(sum(prompt) / len(prompt)) if prompt else 0
        avg_extra = int(sum(extra) / len(extra)) if extra else 0

        cells = []
        for t in TYPES:
            a, b = by_type.get(t, [0, 0])
            cells.append(f"{a}/{b}" if b else "-")
        print(
            f"{name:18s} {ok:3d}/{n:<3d}{100 * ok / n:5.1f}% "
            f"[{lo:4.1f},{hi:4.1f}] "
            f"{cells[0]:>6s} {cells[1]:>6s} {cells[2]:>6s} {cells[3]:>6s} "
            f"{avg_prompt:>9d} {avg_prompt + avg_extra:>9d}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
