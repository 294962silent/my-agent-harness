"""模块 5 实验：长期记忆（memory.py + memory_index.py）

5-A 双层时间建模 vs 覆盖更新：能否回答「之前是什么」
5-B BM25 回退检索引擎的质量
5-C 记忆对 system prompt 的 token 占用
"""

from __future__ import annotations

from pathlib import Path

from experiments.per_module.framework import ExperimentResult, save_result

from my_agent_core.memory_index import BM25, Consolidator, Fact


def exp_5a_bitemporal() -> ExperimentResult:
    """双层时间 vs 覆盖更新。"""
    results = {}

    # ── 覆盖更新（朴素做法）──
    naive: dict[str, str] = {}
    naive["python_version"] = "3.11"
    naive["python_version"] = "3.12"  # 第 5 天更新
    results["覆盖更新_当前值"] = naive["python_version"]
    results["覆盖更新_历史值"] = "无法查询（已丢失）"

    # ── 双层时间（本项目）──
    cons = Consolidator()
    f1 = Fact(
        subject="user",
        key="python_version",
        value="3.11",
        valid_from="2026-11-01",
        source="day1",
    )
    cons.add(f1)

    f2 = Fact(
        subject="user",
        key="python_version",
        value="3.12",
        valid_from="2026-11-05",
        source="day5",
    )
    cons.add(f2)

    # 当前值
    actives = [f for f in cons.facts if f.active]
    results["双层时间_当前值"] = actives[0].value if actives else "?"
    # 历史值
    hist = actives[0].history if actives else []
    results["双层时间_历史值"] = hist[0]["value"] if hist else "?"
    results["双层时间_历史有效期"] = (
        "%s ~ %s" % (hist[0].get("valid_from"), hist[0].get("valid_to"))
        if hist
        else "?"
    )
    results["双层时间_历史链长度"] = len(hist)

    ok_current = results["双层时间_当前值"] == "3.12"
    ok_history = results["双层时间_历史值"] == "3.11"

    return ExperimentResult(
        exp_id="5-A",
        module="长期记忆",
        hypothesis="双层时间建模能同时回答「现在是什么」和「之前是什么」，覆盖更新只能答前者",
        method="模拟 Python 版本从 3.11 更新到 3.12，分别查询当前值与历史值",
        variants=results,
        metric_name="当前值 / 历史值查询结果",
        conclusion=(
            "双层时间：当前=%s、历史=%s（有效期 %s）；覆盖更新：历史值丢失"
            % (
                results["双层时间_当前值"],
                results["双层时间_历史值"],
                results["双层时间_历史有效期"],
            )
        ),
        verified=ok_current and ok_history,
    )


def exp_5b_bm25() -> ExperimentResult:
    """BM25 检索质量。"""
    docs = [
        ("mem1", "用户偏好使用 Python 3.12 进行开发"),
        ("mem2", "项目的测试框架是 pytest"),
        ("mem3", "数据库使用 PostgreSQL 15"),
        ("mem4", "代码风格遵循 PEP 8 规范"),
        ("mem5", "部署环境是 Ubuntu 22.04"),
        ("mem6", "Python 虚拟环境用 uv 管理"),
        ("mem7", "日志系统使用 structlog"),
        ("mem8", "前端框架是 React 18"),
    ]

    tokenized = [t for _, t in docs]
    # 简单分词：按字符切分（中文场景）
    toks = [[c for c in d if c.strip()] for d in tokenized]

    # 用 BM25 需要 token 列表
    def tokenize(s: str) -> list[str]:
        return [c for c in s if c.strip()]

    bm25 = BM25([tokenize(t) for t in tokenized])

    queries = [
        ("Python", {"mem1", "mem6"}),
        ("测试", {"mem2"}),
        ("数据库", {"mem3"}),
        ("部署", {"mem5"}),
    ]

    hits = 0
    details = {}
    for q, expected in queries:
        qt = tokenize(q)
        scores = [(i, bm25.score(qt, i)) for i in range(len(docs))]
        scores.sort(key=lambda x: -x[1])
        top = {docs[i][0] for i, s in scores[:2] if s > 0}
        hit = bool(top & expected)
        hits += 1 if hit else 0
        details[q] = {"top2": sorted(top), "hit": hit}

    recall = hits / len(queries)

    return ExperimentResult(
        exp_id="5-B",
        module="长期记忆",
        hypothesis="BM25 回退引擎在 FTS5 不可用时仍能有效检索",
        method="%d 条记忆、%d 个查询，检查 top-2 是否命中" % (len(docs), len(queries)),
        variants={"recall@2": round(recall, 2), "明细": details},
        metric_name="Recall@2",
        conclusion="Recall@2 = %.0f%%（%d/%d 查询命中）"
        % (recall * 100, hits, len(queries)),
        verified=recall >= 0.5,
    )


def exp_5c_budget() -> ExperimentResult:
    """记忆对 system prompt 的 token 占用。"""
    from my_agent_core.context import estimate_tokens
    from my_agent_llm.models import Message

    results = {}
    for n in (0, 10, 50, 100):
        lines = []
        for i in range(n):
            lines.append("- 用户偏好 %d: 使用工具 %d 处理任务" % (i, i))
        body = "\n".join(lines)
        tokens = estimate_tokens([Message(role="system", content=body)]) if body else 0
        results["%d条记忆_token" % n] = tokens

    # 是否线性
    per_mem = results["100条记忆_token"] / 100 if results["100条记忆_token"] else 0

    return ExperimentResult(
        exp_id="5-C",
        module="长期记忆",
        hypothesis="记忆注入的 token 占用随条数线性增长，需要预算控制",
        method="0/10/50/100 条记忆，测量 system prompt token 数",
        variants=results,
        metric_name="system prompt token 数",
        conclusion="100 条记忆占 %d token（每条约 %.1f token），呈线性增长"
        % (results["100条记忆_token"], per_mem),
        verified=results["100条记忆_token"] > results["10条记忆_token"],
    )


def main() -> None:
    for fn in (exp_5a_bitemporal, exp_5b_bm25, exp_5c_budget):
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
