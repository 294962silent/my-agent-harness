"""真实 API 实验（第二批）：缓存阈值精确测量 + TTL 观测

API-4：二分探测 cache 命中的最小 prompt 长度
API-5：TTL 观测（cache_write_5m / cache_write_1h 的含义）

复现：uv run python -m experiments.per_module.exp_api_threshold
"""

from __future__ import annotations

import json
import pathlib
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

from experiments.per_module.framework import ExperimentResult, save_result

MODEL = "glm-5.3-flash"


def load_creds() -> tuple[str, str]:
    p = pathlib.Path.home() / ".my-pi-agent" / "auth.json"
    prov = json.loads(p.read_text(encoding="utf-8"))["providers"]["openai"]["default"]
    return prov["key"], prov["base_url"].rstrip("/")


KEY, BASE = load_creds()


def chat(
    messages: list[dict[str, Any]],
    session: str | None = None,
    max_tokens: int = 5,
    retries: int = 3,
) -> dict[str, Any]:
    body = json.dumps(
        {"model": MODEL, "messages": messages, "max_tokens": max_tokens}
    ).encode()
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            BASE + "/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + KEY,
                "Content-Type": "application/json",
                "User-Agent": "my-pi-agent/0.1",
                "x-opencode-session": session or str(uuid.uuid4()),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1)
    raise RuntimeError("重试耗尽: %s" % last)


def usage_of(resp: dict[str, Any]) -> dict[str, int]:
    u = resp.get("usage", {}) or {}
    oc = u.get("opencode", {}) or {}
    t = oc.get("tokens", {}) or {}
    return {
        "prompt_tokens": u.get("prompt_tokens", 0),
        "cache_read": t.get("cache_read", 0),
        "uncached": t.get("input", 0),
        "cache_write_5m": t.get("cache_write_5m", 0),
        "cache_write_1h": t.get("cache_write_1h", 0),
    }


UNIT = "这是一段用于精确测量缓存阈值的填充文本。"  # 约 10 token


def build_prompt(target_units: int, tag: str) -> list[dict[str, Any]]:
    """构造约 target_units*10 token 的 prompt，tag 保证内容唯一。"""
    body = (UNIT * target_units) + "[tag:%s]" % tag
    return [
        {"role": "system", "content": "You are helpful. " + body},
        {"role": "user", "content": "OK"},
    ]


def probe_hit(units: int, tag: str) -> tuple[int, int, bool]:
    """测一次：先请求一次建立缓存，再请求一次看是否命中。

    返回 (prompt_tokens, cache_read, 是否命中)。
    """
    msgs = build_prompt(units, tag)
    # 第一次：建立缓存
    r1 = chat(msgs)
    u1 = usage_of(r1)
    time.sleep(0.5)
    # 第二次：检查命中
    r2 = chat(msgs)
    u2 = usage_of(r2)
    return u2["prompt_tokens"], u2["cache_read"], u2["cache_read"] > 0


def exp_api4_threshold() -> ExperimentResult:
    """二分探测：cache 命中的最小 prompt 长度。"""
    tag_base = uuid.uuid4().hex[:8]

    # 先用较粗的网格定位区间
    grid = [50, 100, 150, 200, 250, 300, 400, 500]
    samples: dict[str, dict[str, Any]] = {}
    first_hit_units = None

    for units in grid:
        tag = "%s-%d" % (tag_base, units)
        try:
            pt, cr, hit = probe_hit(units, tag)
            samples["%d单位" % units] = {
                "prompt_tokens": pt,
                "cache_read": cr,
                "命中": hit,
            }
            print("  %3d 单位 → prompt=%5d cache_read=%5d 命中=%s"
                  % (units, pt, cr, hit))
            if hit and first_hit_units is None:
                first_hit_units = units
                break
        except Exception as exc:  # noqa: BLE001
            samples["%d单位" % units] = {"error": type(exc).__name__}
            print("  %3d 单位 → 失败 %s" % (units, type(exc).__name__))

    # 若网格内没找到，继续往上试
    if first_hit_units is None:
        for units in [600, 800, 1000, 1200]:
            tag = "%s-%d" % (tag_base, units)
            try:
                pt, cr, hit = probe_hit(units, tag)
                samples["%d单位" % units] = {
                    "prompt_tokens": pt,
                    "cache_read": cr,
                    "命中": hit,
                }
                print("  %3d 单位 → prompt=%5d cache_read=%5d 命中=%s"
                      % (units, pt, cr, hit))
                if hit:
                    first_hit_units = units
                    break
            except Exception as exc:  # noqa: BLE001
                samples["%d单位" % units] = {"error": type(exc).__name__}

    hit_samples = [
        v["prompt_tokens"] for v in samples.values() if v.get("命中")
    ]
    miss_samples = [
        v["prompt_tokens"] for v in samples.values() if v.get("命中") is False
    ]

    threshold = min(hit_samples) if hit_samples else 0
    max_miss = max(miss_samples) if miss_samples else 0

    return ExperimentResult(
        exp_id="API-4",
        module="真实 API / KV cache",
        hypothesis="provider 缓存存在最小 prompt 长度阈值",
        method="构造 50-1200 单位（约 500-12000 token）的 prompt，各请求两次看是否命中",
        variants={
            "命中样本_prompt_tokens": hit_samples,
            "未命中样本_prompt_tokens": miss_samples,
            "首次命中_prompt_tokens": threshold,
            "最大未命中_prompt_tokens": max_miss,
            "阈值区间": (
                "(%d, %d]" % (max_miss, threshold)
                if threshold and max_miss
                else "未定位"
            ),
            "明细": samples,
        },
        metric_name="触发缓存的最小 prompt 长度",
        conclusion=(
            "阈值落在 (%d, %d] token 区间" % (max_miss, threshold)
            if threshold
            else "在测试范围内未触发缓存"
        ),
        verified=bool(threshold),
    )


def exp_api5_ttl() -> ExperimentResult:
    """TTL 观测：cache_write_5m / cache_write_1h 字段的含义。

    方法：建立缓存后，分别等待 0s / 60s / 180s 再请求，看命中率变化。
    （完整 5 分钟 TTL 需等 300s，这里测前 3 分钟的趋势）
    """
    tag = uuid.uuid4().hex[:10]
    msgs = build_prompt(300, tag)  # 约 3000 token

    # 建立缓存
    r0 = chat(msgs)
    u0 = usage_of(r0)
    t_build = time.time()
    print("  建立缓存: prompt=%d cache_read=%d write_5m=%d write_1h=%d"
          % (u0["prompt_tokens"], u0["cache_read"],
             u0["cache_write_5m"], u0["cache_write_1h"]))

    observations = []
    for wait in [0, 60, 120, 180]:
        if wait > 0:
            time.sleep(wait if wait == 60 else 60)
        r = chat(msgs)
        u = usage_of(r)
        elapsed = time.time() - t_build
        rate = (u["cache_read"] / u["prompt_tokens"] * 100) if u["prompt_tokens"] else 0
        observations.append({
            "elapsed_s": round(elapsed),
            "cache_read": u["cache_read"],
            "hit_rate_%": round(rate, 1),
            "write_5m": u["cache_write_5m"],
            "write_1h": u["cache_write_1h"],
        })
        print("  +%3ds: cache_read=%d 命中=%.1f%% write_5m=%d write_1h=%d"
              % (elapsed, u["cache_read"], rate,
                 u["cache_write_5m"], u["cache_write_1h"]))

    build_write_5m = u0["cache_write_5m"]
    build_write_1h = u0["cache_write_1h"]
    last_rate = observations[-1]["hit_rate_%"]

    return ExperimentResult(
        exp_id="API-5",
        module="真实 API / KV cache",
        hypothesis="cache_write_5m/1h 记录缓存写入量；TTL 内命中率应保持稳定",
        method="建立缓存后，在 0/60/120/180 秒各请求一次，观察命中率与 write 字段",
        variants={
            "建立时_write_5m": build_write_5m,
            "建立时_write_1h": build_write_1h,
            "观测序列": observations,
            "3分钟时命中率_%": last_rate,
        },
        metric_name="TTL 内的命中率稳定性",
        conclusion=(
            "cache_write_5m=%d、cache_write_1h=%d；180 秒后命中率 %.1f%%（%s）"
            % (
                build_write_5m,
                build_write_1h,
                last_rate,
                "仍有效" if last_rate > 50 else "已过期",
            )
        ),
        verified=last_rate > 0,
    )


def main() -> None:
    print("=== 真实 API 第二批实验（%s）===" % MODEL)
    print()

    for name, fn in [
        ("API-4 缓存阈值二分探测", exp_api4_threshold),
        ("API-5 TTL 观测", exp_api5_ttl),
    ]:
        print("[%s]" % name)
        try:
            r = fn()
            save_result(r)
            tag = "OK" if r.verified else "WARN"
            print("[%s] [%s] %s" % (tag, r.exp_id, r.conclusion))
            print()
        except Exception as exc:  # noqa: BLE001
            import traceback

            print("FAIL %s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()
            print()


if __name__ == "__main__":
    main()
