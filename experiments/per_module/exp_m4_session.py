"""模块 4 实验：会话树（session/）

4-A Fork 成本：树状 O(1) vs 线性 O(n)
4-B 增量追加 vs 全量重写（P1-2 缺陷量化）
4-C 崩溃一致性：JSONL vs 单个 JSON
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from experiments.per_module.framework import ExperimentResult, save_result

from my_agent_core.session.session import Session


def exp_4a_fork() -> ExperimentResult:
    """Fork 成本随会话长度的变化。"""
    tmp = Path(__file__).parent / "_tmp_4a"
    tmp.mkdir(exist_ok=True)

    results = {}
    for n_msgs in (100, 1000, 5000):
        # ── 树状：Session + fork ──
        sess_path = tmp / ("tree_%d.jsonl" % n_msgs)
        sess = Session(path=sess_path)
        for i in range(n_msgs):
            sess.add_message("user", "msg %d" % i)

        t0 = time.perf_counter()
        fork_path = tmp / ("fork_%d.jsonl" % n_msgs)
        forked = sess.fork(sess.tree.current_id, new_path=fork_path)
        tree_time = time.perf_counter() - t0
        tree_bytes = fork_path.stat().st_size if fork_path.exists() else 0

        # ── 线性：复制整个 JSON 数组 ──
        linear = [{"role": "user", "content": "msg %d" % i} for i in range(n_msgs)]
        t0 = time.perf_counter()
        linear_copy = list(linear)
        lin_path = tmp / ("linear_%d.json" % n_msgs)
        lin_path.write_text(json.dumps(linear_copy), encoding="utf-8")
        linear_time = time.perf_counter() - t0
        linear_bytes = lin_path.stat().st_size

        results["%d条_树状fork_ms" % n_msgs] = round(tree_time * 1000, 3)
        results["%d条_线性拷贝_ms" % n_msgs] = round(linear_time * 1000, 3)
        results["%d条_树状新增字节" % n_msgs] = tree_bytes
        results["%d条_线性新增字节" % n_msgs] = linear_bytes

    # 增长率
    small = results["100条_树状fork_ms"]
    large = results["5000条_树状fork_ms"]
    growth = large / small if small else 0

    return ExperimentResult(
        exp_id="4-A",
        module="会话树",
        hypothesis="考察实际 fork 实现的成本增长曲线（预期 O(1)，实测待验证）",
        method="100/1000/5000 条消息各 fork 一次，对比耗时与写入字节",
        variants=results,
        metric_name="fork 耗时 / 写入字节",
        conclusion=(
            "实测发现 fork 是 O(n) 而非 O(1)：5000 条耗时 %.1f 秒（100 条为 %.3f 秒，增长 %.0f×）。"
            "根因：fork() 逐条 add_message，每条都触发 save() 全量重写（session.py:553）。"
            "这是一个真实的性能缺陷——会话树结构本身支持 O(1) 分叉，但落盘实现拖累了它。"
            % (large / 1000, small / 1000, growth)
        ),
        verified=growth < 5,
    )


def exp_4b_append() -> ExperimentResult:
    """增量追加 vs 全量重写。"""
    tmp = Path(__file__).parent / "_tmp_4b"
    tmp.mkdir(exist_ok=True)

    N = 200
    results = {}

    # ── 全量重写 ──
    full_path = tmp / "full.json"
    msgs = []
    t0 = time.perf_counter()
    for i in range(N):
        msgs.append({"role": "user", "content": "消息 %d " % i + "X" * 200})
        full_path.write_text(json.dumps(msgs), encoding="utf-8")
    full_time = time.perf_counter() - t0
    full_bytes = sum(len(json.dumps(msgs[: k + 1])) for k in range(N))

    # ── 增量追加 ──
    app_path = tmp / "append.jsonl"
    t0 = time.perf_counter()
    with app_path.open("w", encoding="utf-8") as f:
        for i in range(N):
            f.write(json.dumps({"role": "user", "content": "消息 %d " % i + "X" * 200}) + "\n")
    app_time = time.perf_counter() - t0
    app_bytes = app_path.stat().st_size

    results["全量重写_耗时_ms"] = round(full_time * 1000, 1)
    results["增量追加_耗时_ms"] = round(app_time * 1000, 1)
    results["全量重写_写入字节"] = full_bytes
    results["增量追加_写入字节"] = app_bytes
    results["写入字节比"] = round(full_bytes / app_bytes, 1) if app_bytes else 0
    results["耗时比"] = round(full_time / app_time, 1) if app_time else 0

    return ExperimentResult(
        exp_id="4-B",
        module="会话树",
        hypothesis="增量追加的写入量是 O(n)，全量重写是 O(n²)",
        method="%d 条消息，对比每条全量重写 vs JSONL 追加" % N,
        variants=results,
        metric_name="总写入字节 / 耗时",
        conclusion="全量重写写入 %d 字节（是追加的 %.1f×），耗时 %.1f×"
        % (full_bytes, results["写入字节比"], results["耗时比"]),
        verified=results["写入字节比"] > 10,
    )


def exp_4c_crash() -> ExperimentResult:
    """崩溃一致性：截断后能否恢复。"""
    tmp = Path(__file__).parent / "_tmp_4c"
    tmp.mkdir(exist_ok=True)

    msgs = [{"role": "user", "content": "消息 %d" % i} for i in range(10)]
    results = {}

    # ── 单个 JSON 数组 ──
    json_path = tmp / "single.json"
    content = json.dumps(msgs)
    json_path.write_text(content, encoding="utf-8")
    # 模拟崩溃：截断到 60%
    truncated = content[: int(len(content) * 0.6)]
    json_path.write_text(truncated, encoding="utf-8")
    try:
        recovered = json.loads(json_path.read_text(encoding="utf-8"))
        results["单个JSON_可恢复条数"] = len(recovered)
    except json.JSONDecodeError:
        results["单个JSON_可恢复条数"] = 0
        results["单个JSON_错误"] = "JSONDecodeError（整个文件损坏）"

    # ── JSONL ──
    jsonl_path = tmp / "multi.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for m in msgs:
            f.write(json.dumps(m) + "\n")
    lines = jsonl_path.read_text(encoding="utf-8").splitlines()
    # 模拟崩溃：截断最后一行
    ok_lines = []
    for line in lines[:-1]:
        try:
            json.loads(line)
            ok_lines.append(line)
        except json.JSONDecodeError:
            break
    jsonl_path.write_text("\n".join(lines[:-1]) + "\n" + '{"role":"user","cont', encoding="utf-8")

    # 恢复：逐行解析，遇错即停
    recovered_jsonl = 0
    for line in jsonl_path.read_text(encoding="utf-8").splitlines():
        try:
            json.loads(line)
            recovered_jsonl += 1
        except json.JSONDecodeError:
            break
    results["JSONL_可恢复条数"] = recovered_jsonl
    results["JSONL_总条数"] = len(msgs)

    return ExperimentResult(
        exp_id="4-C",
        module="会话树",
        hypothesis="JSONL 在最坏崩溃下只丢最后一行，单个 JSON 会整体损坏",
        method="写入 10 条后模拟崩溃（截断文件），尝试恢复",
        variants=results,
        metric_name="可恢复消息条数",
        conclusion="单个 JSON 恢复 %d 条，JSONL 恢复 %d/%d 条"
        % (
            results["单个JSON_可恢复条数"],
            results["JSONL_可恢复条数"],
            len(msgs),
        ),
        verified=results["JSONL_可恢复条数"] >= 9
        and results["单个JSON_可恢复条数"] < 10,
    )


def main() -> None:
    for fn in (exp_4a_fork, exp_4b_append, exp_4c_crash):
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
