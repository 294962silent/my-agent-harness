"""T4：数据变换。

给定输入文件和变换规则，要求产出一个精确的输出文件。
判定：**逐字节比对**期望输出——完全确定性，天然的 verifiable reward。
"""

from __future__ import annotations

from experiments.agent_rl.task import Task, register

_CHECK = [
    "python",
    "-c",
    "import sys,pathlib; exp=pathlib.Path('expected.txt').read_text(); got=pathlib.Path('out.txt').read_text() if pathlib.Path('out.txt').exists() else ''; sys.exit(0 if got==exp else 1)",
]

# (变体名, 规则描述, 输入文件, 期望输出, 是否 hard)
_CASES: list[tuple[str, str, dict[str, str], str, bool]] = [
    (
        "csv_to_tsv",
        "把 CSV（逗号分隔）转成 TSV（制表符分隔），保持行列顺序",
        {"input.csv": "a,b,c\n1,2,3\n"},
        "a\tb\tc\n1\t2\t3\n",
        False,
    ),
    (
        "sort_lines",
        "把每行按字典序升序排序",
        {"input.txt": "banana\napple\ncherry\n"},
        "apple\nbanana\ncherry\n",
        False,
    ),
    (
        "dedupe_lines",
        "去重并保持首次出现顺序",
        {"input.txt": "a\nb\na\nc\nb\n"},
        "a\nb\nc\n",
        False,
    ),
    (
        "uppercase_first_letter",
        "把每行首字母大写，其余不变",
        {"input.txt": "alpha\nbeta\n"},
        "Alpha\nBeta\n",
        False,
    ),
    (
        "sum_columns",
        "把每行两个数字相加，输出 和",
        {"input.txt": "1 2\n3 4\n"},
        "3\n7\n",
        False,
    ),
    (
        "json_pretty",
        "把压缩 JSON 转成缩进 2 空格的格式（保持键顺序）",
        {"input.json": '{"a":1,"b":2}'},
        '{\n  "a": 1,\n  "b": 2\n}',
        True,
    ),
    (
        "rotate_grid",
        "把字符网格顺时针旋转 90 度",
        {"input.txt": "ab\ncd\n"},
        "ca\ndb\n",
        True,
    ),
    (
        "word_freq_top3",
        "统计词频，取前 3 名，格式 '词 次数'，按次数降序、同频按字母序",
        {"input.txt": "b a b c a b\n"},
        "b 3\na 2\nc 1\n",
        True,
    ),
    (
        "merge_sorted",
        "合并两个已排序文件为一个有序文件",
        {"a.txt": "1\n3\n5\n", "b.txt": "2\n4\n6\n"},
        "1\n2\n3\n4\n5\n6\n",
        True,
    ),
    (
        "run_length_encode",
        "把连续重复字符编码成 字符+次数",
        {"input.txt": "aaabbc\n"},
        "a3b2c1\n",
        True,
    ),
    (
        "reverse_words",
        "把每行的单词顺序反转",
        {"input.txt": "a b c\nd e\n"},
        "c b a\ne d\n",
        False,
    ),
    (
        "strip_blank",
        "删除空行（全空白的行）",
        {"input.txt": "a\n\nb\n  \nc\n"},
        "a\nb\nc\n",
        False,
    ),
    (
        "number_lines",
        "给每行加行号，格式 `N: 内容`（从 1 开始）",
        {"input.txt": "a\nb\n"},
        "1: a\n2: b\n",
        False,
    ),
    (
        "csv_select_col",
        "只保留 CSV 的第 1 列和第 3 列（逗号分隔）",
        {"input.csv": "a,b,c\n1,2,3\n"},
        "a,c\n1,3\n",
        False,
    ),
    (
        "replace_word",
        "把所有 `foo` 替换成 `bar`",
        {"input.txt": "foo x foo\n"},
        "bar x bar\n",
        False,
    ),
    (
        "group_and_count",
        "按第一列分组，输出 `键 数量`，按数量降序、同数量按键字母序",
        {"input.txt": "x 1\ny 1\nx 2\n"},
        "x 2\ny 1\n",
        True,
    ),
    (
        "transpose_matrix",
        "把空格分隔的数字矩阵转置",
        {"input.txt": "1 2 3\n4 5 6\n"},
        "1 4\n2 5\n3 6\n",
        True,
    ),
    (
        "dedupe_keep_last",
        "按行去重，保留**最后一次**出现的位置",
        {"input.txt": "a\nb\na\nc\nb\n"},
        "a\nc\nb\n",
        True,
    ),
    (
        "parse_log_levels",
        "从日志行中提取级别（`[INFO]`/`[WARN]`），输出每行一个级别",
        {"input.txt": "[INFO] start\n[WARN] slow\n[INFO] done\n"},
        "INFO\nWARN\nINFO\n",
        True,
    ),
    (
        "csv_join",
        "按第 1 列把两个 CSV 连接（左连接），输出 `key,v1,v2`；缺失填 NA",
        {"a.csv": "k1,10\nk2,20\n", "b.csv": "k1,foo\n"},
        "k1,10,foo\nk2,20,NA\n",
        True,
    ),
]


def _build(index: int) -> Task:
    name, rule, files, expected, hard = _CASES[index]
    payload = dict(files)
    payload["expected.txt"] = expected
    payload["README.md"] = f"# 任务\n\n{rule}。\n\n把结果写入 `out.txt`，需与 `expected.txt` 完全一致。\n"
    return Task(
        task_id=f"t4-{index + 1:03d}",
        template="t4_transform",
        difficulty="hard" if hard else "easy",
        prompt=(
            f"{rule}。\n读输入文件，把结果写入 `out.txt`；要求与 `expected.txt` 逐字节一致。\n"
            "不要修改 expected.txt。"
        ),
        files=payload,
        check_cmd=_CHECK,
        meta={"case": name},
    )


@register("t4_transform")
def generate() -> list[Task]:
    """20 个数据变换任务：10 easy + 10 hard。"""
    return [_build(i) for i in range(len(_CASES))]
