"""T5：命令行工具实现。

要求实现一个可执行的 CLI（参数解析、退出码、stdout 格式）。
判定：一组 shell 断言——同时检查**标准输出**与**退出码**。
"""

from __future__ import annotations

from experiments.agent_rl.task import Task, register

_CHECK = ["sh", "-lc", "sh check.sh"]

# (变体名, 规格描述, 需要的文件, check.sh 内容, 是否 hard)
_CASES: list[tuple[str, str, dict[str, str], str, bool]] = [
    (
        "echo_args",
        "`python cli.py` 打印全部位置参数，以空格分隔",
        {},
        'test "$(python cli.py a b c)" = "a b c"',
        False,
    ),
    (
        "exit_code_on_empty",
        "`python cli.py` 无参数时退出码为 2，有参数时为 0",
        {},
        'python cli.py >/dev/null 2>&1; test $? -eq 2 && python cli.py x >/dev/null; test $? -eq 0',
        False,
    ),
    (
        "sum_numbers",
        "`python cli.py sum 1 2 3` 输出 6",
        {},
        'test "$(python cli.py sum 1 2 3)" = "6"',
        False,
    ),
    (
        "count_lines",
        "`python cli.py count file.txt` 输出文件行数",
        {"data.txt": "a\nb\nc\n"},
        'test "$(python cli.py count data.txt)" = "3"',
        False,
    ),
    (
        "greet_flag",
        "`python cli.py --name Ada` 输出 `Hello, Ada!`",
        {},
        'test "$(python cli.py --name Ada)" = "Hello, Ada!"',
        False,
    ),
    (
        "word_count_file",
        "`python cli.py wc file.txt` 输出 `lines words`（以空格分隔）",
        {"data.txt": "hello world\nfoo\n"},
        'test "$(python cli.py wc data.txt)" = "2 3"',
        True,
    ),
    (
        "filter_lines",
        "`python cli.py grep pat file` 输出包含 pat 的行；无匹配时退出码 1",
        {"data.txt": "apple\nbanana\napricot\n"},
        'test "$(python cli.py grep ap data.txt)" = "apple\napricot" -o "$(python cli.py grep ap data.txt)" = "$(printf \'apple\\napricot\')"',
        True,
    ),
    (
        "csv_column",
        "`python cli.py col 2 file.csv` 输出第 2 列（不含表头）",
        {"data.csv": "name,age\nada,36\nbob,41\n"},
        'test "$(python cli.py col 2 data.csv)" = "36\n41" -o "$(python cli.py col 2 data.csv)" = "$(printf \'36\\n41\')"',
        True,
    ),
    (
        "json_field",
        "`python cli.py get name data.json` 输出 name 字段的值",
        {"data.json": '{"name": "ada", "age": 36}'},
        'test "$(python cli.py get name data.json)" = "ada"',
        True,
    ),
    (
        "top_words",
        "`python cli.py top 2 file.txt` 输出出现最多的 2 个词，每行一个，按次数降序",
        {"data.txt": "a b a c a b\n"},
        'test "$(python cli.py top 2 data.txt)" = "a\nb" -o "$(python cli.py top 2 data.txt)" = "$(printf \'a\\nb\')"',
        True,
    ),
    (
        "print_version",
        "`python cli.py --version` 输出 `v1.0`",
        {},
        'test "$(python cli.py --version)" = "v1.0"',
        False,
    ),
    (
        "upper_arg",
        "`python cli.py upper abc` 输出 `ABC`",
        {},
        'test "$(python cli.py upper abc)" = "ABC"',
        False,
    ),
    (
        "repeat_flag",
        "`python cli.py repeat 3 x` 输出 `xxx`",
        {},
        'test "$(python cli.py repeat 3 x)" = "xxx"',
        False,
    ),
    (
        "file_exists_check",
        "`python cli.py exists f` 文件存在输出 yes、否则 no",
        {"here.txt": "x"},
        'test "$(python cli.py exists here.txt)" = "yes" && test "$(python cli.py exists nope.txt)" = "no"',
        False,
    ),
    (
        "sum_stdin",
        "`echo 1 2 3 | python cli.py sumstdin` 输出 6",
        {},
        'test "$(echo 1 2 3 | python cli.py sumstdin)" = "6"',
        False,
    ),
    (
        "sort_unique_stdin",
        "`python cli.py uniq` 从 stdin 读行，排序去重后输出",
        {},
        'test "$(printf \'b\\na\\nb\' | python cli.py uniq)" = "$(printf \'a\\nb\')"',
        True,
    ),
    (
        "extract_numbers",
        "`python cli.py nums file` 输出文件中所有整数，每行一个",
        {"data.txt": "a1 b22 c333"},
        'test "$(python cli.py nums data.txt)" = "$(printf \'1\\n22\\n333\')"',
        True,
    ),
    (
        "config_merge",
        "`python cli.py merge a.json b.json` 输出合并后的 JSON（b 覆盖 a）",
        {"a.json": '{"x": 1, "y": 2}', "b.json": '{"y": 3}'},
        'python cli.py merge a.json b.json | python -c "import json,sys; d=json.load(sys.stdin); sys.exit(0 if d=={\'x\':1,\'y\':3} else 1)"',
        True,
    ),
    (
        "tail_n",
        "`python cli.py tail 2 file` 输出最后 2 行",
        {"data.txt": "a\nb\nc\n"},
        'test "$(python cli.py tail 2 data.txt)" = "$(printf \'b\\nc\')"',
        True,
    ),
    (
        "csv_sum_column",
        "`python cli.py csvsum 2 file.csv` 输出第 2 列数值之和（跳过表头）",
        {"data.csv": "name,score\na,10\nb,20\n"},
        'test "$(python cli.py csvsum 2 data.csv)" = "30"',
        True,
    ),
]


def _build(index: int) -> Task:
    name, spec, files, check, hard = _CASES[index]
    payload = dict(files)
    payload["check.sh"] = check + "\n"
    payload["README.md"] = f"# 任务\n\n实现 `cli.py`：{spec}\n\n判定脚本 `check.sh` 会检查输出与退出码。\n"
    return Task(
        task_id=f"t5-{index + 1:03d}",
        template="t5_cli",
        difficulty="hard" if hard else "easy",
        prompt=(
            f"实现命令行工具 `cli.py`：{spec}\n"
            "用 Python 标准库即可（argparse 或手写解析）。\n"
            "判定由 `sh check.sh` 完成，可以读它了解具体要求。"
        ),
        files=payload,
        check_cmd=_CHECK,
        meta={"case": name},
    )


@register("t5_cli")
def generate() -> list[Task]:
    """20 个 CLI 任务：10 easy + 10 hard。"""
    return [_build(i) for i in range(len(_CASES))]
