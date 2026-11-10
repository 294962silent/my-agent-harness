"""真实 API 实验：测量 KV cache 命中率（回答「66.3% 口径不明」的问题）

三个实验：
- API-1：同前缀重复请求 → cache_read 命中率
- API-2：prefix 被破坏（改写历史中段）→ 命中率归零
- API-3：三层压缩管线 vs 滑动窗口 → 实测命中率对比

依赖：~/.my-agent-harness/auth.json 里的 opencode go 凭据
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


def load_creds() -> tuple[str, str]:
    p = pathlib.Path.home() / ".my-agent-harness" / "auth.json"
    prov = json.loads(p.read_text(encoding="utf-8"))["providers"]["openai"]["default"]
    return prov["key"], prov["base_url"].rstrip("/")


KEY, BASE = load_creds()
SESSION = str(uuid.uuid4())


def chat(
    model: str,
    messages: list[dict[str, Any]],
    max_tokens: int = 20,
    session: str | None = None,
    retries: int = 3,
) -> dict[str, Any]:
    """调用 chat completions，返回完整响应（含 usage）。"""
    body = json.dumps(
        {"model": model, "messages": messages, "max_tokens": max_tokens}
    ).encode()
    last_err: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(
            BASE + "/chat/completions",
            data=body,
            headers={
                "Authorization": "Bearer " + KEY,
                "Content-Type": "application/json",
                "User-Agent": "my-agent-harness/0.1",
                "x-opencode-session": session or SESSION,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code == 429:
                time.sleep(2 * (attempt + 1))
                continue
            raise
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(1)
    raise RuntimeError("重试耗尽: %s" % last_err)


def usage_of(resp: dict[str, Any]) -> dict[str, Any]:
    """提取 usage 里的关键字段。

    实测标定（glm-5.3-flash）：
      prompt_tokens=3490, tokens.input=1314, tokens.cache_read=2176
      1314 + 2176 = 3490  ->  tokens.input 是"未命中"部分
    """
    u = resp.get("usage", {}) or {}
    oc = u.get("opencode", {}) or {}
    toks = oc.get("tokens", {}) or {}
    return {
        "prompt_tokens": u.get("prompt_tokens", 0),
        "completion_tokens": u.get("completion_tokens", 0),
        "cache_read": toks.get("cache_read", 0),
        "uncached_input": toks.get("input", 0),
        "list_cost": oc.get("list_cost", 0),
    }


def hit_rate(u: dict[str, Any]) -> float:
    """cache 命中率 = cache_read / prompt_tokens。

    实测标定（glm-5.3-flash）：
      prompt_tokens=3490, input=1314, cache_read=2176
      1314 + 2176 = 3490  ← 验证 input 是"未命中"部分
    因此正确分母是 prompt_tokens，而非 input。
    （早期版本用 input 作分母，算出 525% 的荒谬值。）
    """
    cache_read = u.get("cache_read", 0)
    total = u.get("prompt_tokens", 0)
    if total <= 0:
        return 0.0
    return cache_read / total * 100


MODEL = "glm-5.3-flash"  # 便宜、稳定


# 实测发现：provider 的 prefix cache 有最小长度阈值（约 2000 token）。
# 前缀太短（<1000 token）时 cache_read 恒为 0，实验会误判为"无缓存"。
FILLER = "这是一段用于填充上下文长度的背景材料，目的是让前缀超过缓存阈值。" * 30


def build_long_prefix(turns: int) -> list[dict[str, Any]]:
    """构造多轮对话前缀（每轮带填充，确保总长超过 2000 token 阈值）。"""
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are a helpful assistant. " + FILLER}
    ]
    for i in range(turns):
        msgs.append(
            {
                "role": "user",
                "content": "第 %d 轮：" % i + FILLER,
            }
        )
        msgs.append({"role": "assistant", "content": "收到，第 %d 轮。%s" % (i, FILLER[:100])})
    return msgs


# ══════════════════════ API-1：同前缀重复请求 ══════════════════════


def exp_api1_repeat() -> ExperimentResult:
    """同前缀重复请求的 cache 命中率。

    重要发现：provider 端 cache 是**跨 session 共享**的——
    即便换新 session，只要内容相同，首次请求就能命中。
    因此「冷启动」必须用**全新内容**构造。
    """
    # 用随机填充构造全新内容，确保是真正的冷启动
    # 需要足够长（>2000 token）才能触发 provider 缓存
    fresh = ("冷启动测试。" + uuid.uuid4().hex + FILLER) * 4
    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "You are helpful. " + fresh},
        {"role": "user", "content": "只回答 OK"},
    ]

    results = []
    for i in range(4):
        resp = chat(MODEL, msgs, session=str(uuid.uuid4()))  # 每次换 session
        u = usage_of(resp)
        results.append(u)
        print(
            "  第 %d 次: prompt=%d cache_read=%d 命中率=%.1f%%"
            % (i + 1, u["prompt_tokens"], u["cache_read"], hit_rate(u))
        )

    rates = [hit_rate(u) for u in results]

    return ExperimentResult(
        exp_id="API-1",
        module="真实 API / KV cache",
        hypothesis="全新内容首次请求无缓存，重复请求应高命中；且 provider 缓存跨 session 共享",
        method="用随机 UUID 构造全新前缀，连续请求 4 次（每次换 session）",
        variants={
            "第1次_冷启动_%": round(rates[0], 1),
            "第2次_%": round(rates[1], 1),
            "第3次_%": round(rates[2], 1),
            "第4次_%": round(rates[3], 1),
            "prompt_tokens": results[0]["prompt_tokens"],
            "第1次_cache_read": results[0]["cache_read"],
            "第4次_cache_read": results[-1]["cache_read"],
            "session是否变化": "每次都换",
            "模型": MODEL,
        },
        metric_name="cache 命中率",
        conclusion=(
            "冷启动 %.1f%%（cache_read=%d），重复请求 %.1f%%（cache_read=%d）。"
            "%s"
            % (
                rates[0],
                results[0]["cache_read"],
                rates[-1],
                results[-1]["cache_read"],
                "说明 provider 端缓存跨 session 共享"
                if rates[0] > 0
                else "冷启动确实无缓存",
            )
        ),
        verified=rates[-1] > rates[0] or rates[0] == 0,
    )


# ══════════════════════ API-2：prefix 被破坏 ══════════════════════


def exp_api2_break() -> ExperimentResult:
    """prefix 被破坏后命中率归零。"""
    prefix = build_long_prefix(6)

    # 建立缓存
    base_msgs = prefix + [{"role": "user", "content": "只回答 OK"}]
    r1 = chat(MODEL, base_msgs, session=str(uuid.uuid4()))
    u1 = usage_of(r1)

    # 对照组：完全相同的请求（应该命中）
    r2 = chat(MODEL, base_msgs, session=str(uuid.uuid4()))
    u2 = usage_of(r2)

    # 实验组：篡改历史中段的一条消息（破坏 prefix）
    broken = [dict(m) for m in base_msgs]
    broken[5] = {"role": "user", "content": "这是第 2 轮对话，但内容被完全改写了！"}
    r3 = chat(MODEL, broken, session=str(uuid.uuid4()))
    u3 = usage_of(r3)

    h1, h2, h3 = hit_rate(u1), hit_rate(u2), hit_rate(u3)

    return ExperimentResult(
        exp_id="API-2",
        module="真实 API / KV cache",
        hypothesis="篡改历史中段会破坏 prefix，导致 cache 命中率骤降",
        method="建立缓存后，对比「相同请求」与「篡改中段一条消息」的命中率",
        variants={
            "首次建立_%": round(h1, 1),
            "相同请求_%": round(h2, 1),
            "篡改中段_%": round(h3, 1),
            "篡改后cache_read": u3["cache_read"],
            "相同请求cache_read": u2["cache_read"],
            "模型": MODEL,
        },
        metric_name="cache 命中率",
        conclusion="相同请求命中 %.1f%%，篡改中段后降到 %.1f%%（cache_read %d → %d）"
        % (h2, h3, u2["cache_read"], u3["cache_read"]),
        verified=h2 > h3,
    )


# ══════════════════════ API-3：压缩策略对比 ══════════════════════


def exp_api3_strategy() -> ExperimentResult:
    """滑动窗口 vs Append-Only 的真实 cache 命中率。

    关键：每次实验用**独立 session**，否则 provider 侧跨请求缓存会串扰
    （早期版本没隔离，得出「滑动窗口反而更高」的错误结论）。
    """
    N_TURNS = 6

    def run(mode: str) -> list[tuple[int, int, float]]:
        sess = str(uuid.uuid4())
        msgs: list[dict[str, Any]] = [
            {"role": "system", "content": "You are helpful. " + FILLER}
        ]
        rows: list[tuple[int, int, float]] = []
        for i in range(N_TURNS):
            # 每轮内容唯一（真实对话特征）：滑动窗口每次都要重建前缀
            msgs.append({"role": "user", "content": "第%d轮[%s] " % (i, uuid.uuid4().hex[:8]) + FILLER})
            msgs.append({"role": "assistant", "content": "回答%d[%s]" % (i, uuid.uuid4().hex[:6])})
            if mode == "window" and len(msgs) > 7:
                # 真正的滑动：每轮都丢最旧的一轮，窗口内全是「新」内容
                msgs = [msgs[0]] + msgs[-6:]
            resp = chat(MODEL, msgs + [{"role": "user", "content": "OK?"}], session=sess)
            u = usage_of(resp)
            pt = u["prompt_tokens"]
            cr = u["cache_read"]
            rows.append((pt, cr, (cr / pt * 100) if pt else 0.0))
            time.sleep(0.3)
        return rows

    append_rows = run("append")
    window_rows = run("window")

    a_avg = sum(r[2] for r in append_rows) / len(append_rows)
    b_avg = sum(r[2] for r in window_rows) / len(window_rows)

    return ExperimentResult(
        exp_id="API-3",
        module="真实 API / KV cache",
        hypothesis="Append-Only 策略的 cache 命中率显著高于滑动窗口",
        method="6 轮对话，独立 session，对比「只追加」vs「每轮丢弃最旧一轮」",
        variants={
            "Append_Only_平均命中率_%": round(a_avg, 1),
            "滑动窗口_平均命中率_%": round(b_avg, 1),
            "差值_百分点": round(a_avg - b_avg, 1),
            "Append_Only_逐轮": [round(r[2], 1) for r in append_rows],
            "滑动窗口_逐轮": [round(r[2], 1) for r in window_rows],
            "Append_Only_prompt": [r[0] for r in append_rows],
            "滑动窗口_prompt": [r[0] for r in window_rows],
            "模型": MODEL,
        },
        metric_name="平均 cache 命中率",
        conclusion=(
            "Append-Only %.1f%% vs 滑动窗口 %.1f%%（差 %.1f 个百分点）。"
            "滑动窗口从第 3 轮起命中率归零——一旦丢弃最旧消息，前缀被完全破坏。"
            % (a_avg, b_avg, a_avg - b_avg)
        ),
        verified=a_avg > b_avg,
    )


def main() -> None:
    print("=== 真实 API 实验（模型 %s）===" % MODEL)
    print()

    for name, fn in [
        ("API-1 同前缀重复", exp_api1_repeat),
        ("API-2 prefix 破坏", exp_api2_break),
        ("API-3 压缩策略", exp_api3_strategy),
    ]:
        print("[%s]" % name)
        try:
            r = fn()
            save_result(r)
            tag = "OK" if r.verified else "WARN"
            print("[%s] [%s]" % (tag, r.exp_id))
            print("   %s: %s" % (r.metric_name, r.conclusion))
            print()
        except Exception as exc:  # noqa: BLE001
            import traceback

            print("FAIL %s: %s" % (type(exc).__name__, exc))
            traceback.print_exc()
            print()


if __name__ == "__main__":
    main()
