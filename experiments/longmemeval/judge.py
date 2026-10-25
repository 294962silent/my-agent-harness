"""LongMemEval 判分：复用官方 evaluate_qa.py 的 prompt 模板，改用本项目的模型做 judge。

官方脚本：https://github.com/xiaowu0162/LongMemEval/blob/main/src/evaluation/evaluate_qa.py
本实现只替换了 judge 模型（DeepSeek-V4.1-Flash），prompt 一字未改。

用法：
    uv run python experiments/longmemeval/judge.py results/hyp_full_20.jsonl
    uv run python experiments/longmemeval/judge.py results/hyp_full_20.jsonl --out results/judged.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.longmemeval.common import (  # noqa: E402
    DEFAULT_MODEL,
    load_dataset,
    make_client,
)

# ── 以下 prompt 与官方 evaluate_qa.py::get_anscheck_prompt 完全一致 ──────────────

_YES_NO_TEMPLATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response is equivalent to the correct answer or contains all the intermediate steps "
    "to get the correct answer, you should also answer yes. If the response only contains a subset "
    "of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\n"
    "Model Response: {}\n\nIs the model response correct? Answer yes or no only."
)

_TEMPORAL_TEMPLATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response is equivalent to the correct answer or contains all the intermediate steps "
    "to get the correct answer, you should also answer yes. If the response only contains a subset "
    "of the information required by the answer, answer no. In addition, do not penalize off-by-one "
    "errors for the number of days. If the question asks for the number of days/weeks/months, etc., "
    "and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), "
    "the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\n"
    "Model Response: {}\n\nIs the model response correct? Answer yes or no only."
)

_KNOWLEDGE_UPDATE_TEMPLATE = (
    "I will give you a question, a correct answer, and a response from a model. "
    "Please answer yes if the response contains the correct answer. Otherwise, answer no. "
    "If the response contains some previous information along with an updated answer, "
    "the response should be considered as correct as long as the updated answer is the required answer."
    "\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\n"
    "Is the model response correct? Answer yes or no only."
)

_PREFERENCE_TEMPLATE = (
    "I will give you a question, a rubric for desired personalized response, and a response from a model. "
    "Please answer yes if the response satisfies the desired response. Otherwise, answer no. "
    "The model does not need to reflect all the points in the rubric. The response is correct as long as "
    "it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\n"
    "Model Response: {}\n\nIs the model response correct? Answer yes or no only."
)

_ABSTENTION_TEMPLATE = (
    "I will give you an unanswerable question, an explanation, and a response from a model. "
    "Please answer yes if the model correctly identifies the question as unanswerable. "
    "The model could say that the information is incomplete, or some other information is given "
    "but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\n"
    "Does the model correctly identify the question as unanswerable? Answer yes or no only."
)


def build_judge_prompt(task: str, question: str, answer: str, response: str) -> str:
    if task in ("single-session-user", "single-session-assistant", "multi-session"):
        return _YES_NO_TEMPLATE.format(question, answer, response)
    if task == "temporal-reasoning":
        return _TEMPORAL_TEMPLATE.format(question, answer, response)
    if task == "knowledge-update":
        return _KNOWLEDGE_UPDATE_TEMPLATE.format(question, answer, response)
    if task == "single-session-preference":
        return _PREFERENCE_TEMPLATE.format(question, answer, response)
    raise NotImplementedError(f"未知题型: {task}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("hyp_file", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dataset", default="auto", choices=["auto", "oracle", "s", "m"])
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    if args.dataset == "auto":
        args.dataset = "s" if "_s_" in args.hyp_file.name else "oracle"
    ref = {r["question_id"]: r for r in load_dataset(args.dataset)}
    hyps = [json.loads(line) for line in args.hyp_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    client = make_client()

    out_path = args.out or args.hyp_file.with_suffix(".judged.jsonl")
    stats: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    judged = []

    with out_path.open("w", encoding="utf-8") as fh:
        for i, item in enumerate(hyps, 1):
            record = ref.get(item["question_id"])
            if record is None:
                continue
            prompt = build_judge_prompt(
                record["question_type"], record["question"], record["answer"], item["hypothesis"]
            )
            try:
                resp = client.chat.completions.create(
                    model=args.model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.0,
                    # 该模型会先输出 reasoning 再给 content，预算给小了会把 content 截没
                    max_tokens=256,
                )
                choice = resp.choices[0].message
                content = (choice.content or "").strip()
                if not content:  # 兜底：个别情况下只有 reasoning 落盘
                    content = (getattr(choice, "reasoning_content", "") or "").strip()
                verdict = content
                lowered = content.lower()
                correct = lowered.startswith("yes") or "\nyes" in lowered[:40]
            except Exception as exc:  # noqa: BLE001
                verdict = f"error: {exc}"
                correct = False

            qtype = record["question_type"]
            stats[qtype][1] += 1
            stats[qtype][0] += int(correct)
            judged.append({**item, "judge": verdict, "correct": correct})
            fh.write(json.dumps(judged[-1], ensure_ascii=False) + "\n")
            if i % 10 == 0:
                print(f"judged {i}/{len(hyps)}", file=sys.stderr)

    total = [sum(v[0] for v in stats.values()), sum(v[1] for v in stats.values())]
    print(f"\n=== {args.hyp_file.name} ===")
    for qtype, (ok, n) in sorted(stats.items()):
        print(f"  {qtype:26s} {ok:3d}/{n:<3d}  {100.0 * ok / max(1, n):5.1f}%")
    print(f"  {'TOTAL':26s} {total[0]:3d}/{total[1]:<3d}  {100.0 * total[0] / max(1, total[1]):5.1f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
