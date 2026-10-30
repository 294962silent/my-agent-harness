"""为 T3/T4/T5 补参考解法，并验证「参考解法能通过」。

T3：参考解法 = 把旧符号替换成新符号后的完整文件集合。
T4：参考解法 = 一个 `make.py` 脚本，跑完生成 out.txt。
T5：参考解法 = 一个 `cli.py`。

跑法：
    uv run python -m experiments.agent_rl.add_references --template t4_transform
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from experiments.agent_rl.sandbox import Sandbox  # noqa: E402
from experiments.agent_rl.task import Task, generate_all  # noqa: E402
from experiments.agent_rl import tasks as _tasks  # noqa: E402,F401


# ── T3：替换旧符号 ────────────────────────────────────────────────────────────

def t3_reference(task: Task) -> dict[str, str]:
    old = task.meta["old"]
    new = task.meta["new"]
    ref: dict[str, str] = {}
    for rel, content in task.files.items():
        if rel.startswith("test_") or rel == "README.md":
            continue
        if rel.endswith(".py") and old in content:
            ref[rel] = content.replace(old, new)
    return ref


# ── T4：生成脚本 ──────────────────────────────────────────────────────────────

T4_SCRIPTS: dict[str, str] = {
    "csv_to_tsv": "import pathlib\nrows = pathlib.Path('input.csv').read_text().splitlines()\npathlib.Path('out.txt').write_text('\\n'.join(','.join(r.split(',')) .replace(',', '\\t') for r in rows) + '\\n')\n",
    "sort_lines": "import pathlib\nlines = pathlib.Path('input.txt').read_text().splitlines()\npathlib.Path('out.txt').write_text('\\n'.join(sorted(lines)) + '\\n')\n",
    "dedupe_lines": "import pathlib\nseen=[]\nfor line in pathlib.Path('input.txt').read_text().splitlines():\n    if line not in seen: seen.append(line)\npathlib.Path('out.txt').write_text('\\n'.join(seen) + '\\n')\n",
    "uppercase_first_letter": "import pathlib\nlines=[l[:1].upper()+l[1:] for l in pathlib.Path('input.txt').read_text().splitlines()]\npathlib.Path('out.txt').write_text('\\n'.join(lines) + '\\n')\n",
    "sum_columns": "import pathlib\nout=[str(sum(int(x) for x in l.split())) for l in pathlib.Path('input.txt').read_text().splitlines()]\npathlib.Path('out.txt').write_text('\\n'.join(out) + '\\n')\n",
    "json_pretty": "import json, pathlib\nd=json.loads(pathlib.Path('input.json').read_text())\npathlib.Path('out.txt').write_text(json.dumps(d, indent=2))\n",
    "rotate_grid": "import pathlib\nrows=[l for l in pathlib.Path('input.txt').read_text().splitlines()]\nout=[''.join(rows[r][c] for r in range(len(rows)-1,-1,-1)) for c in range(len(rows[0]))]\npathlib.Path('out.txt').write_text('\\n'.join(out) + '\\n')\n",
    "word_freq_top3": "import pathlib, collections\nwords=pathlib.Path('input.txt').read_text().split()\nc=collections.Counter(words)\ntop=sorted(c.items(), key=lambda kv:(-kv[1], kv[0]))[:3]\npathlib.Path('out.txt').write_text('\\n'.join(f'{w} {n}' for w,n in top) + '\\n')\n",
    "merge_sorted": "import pathlib\na=pathlib.Path('a.txt').read_text().split()\nb=pathlib.Path('b.txt').read_text().split()\nvals=sorted(int(x) for x in a+b)\npathlib.Path('out.txt').write_text('\\n'.join(str(v) for v in vals) + '\\n')\n",
    "run_length_encode": "import pathlib, itertools\ns=pathlib.Path('input.txt').read_text().strip()\nout=''.join(f'{ch}{len(list(g))}' for ch,g in itertools.groupby(s))\npathlib.Path('out.txt').write_text(out + '\\n')\n",
    "reverse_words": "import pathlib\nlines=[' '.join(reversed(l.split())) for l in pathlib.Path('input.txt').read_text().splitlines()]\npathlib.Path('out.txt').write_text('\\n'.join(lines) + '\\n')\n",
    "strip_blank": "import pathlib\nlines=[l for l in pathlib.Path('input.txt').read_text().splitlines() if l.strip()]\npathlib.Path('out.txt').write_text('\\n'.join(lines) + '\\n')\n",
    "number_lines": "import pathlib\nlines=pathlib.Path('input.txt').read_text().splitlines()\npathlib.Path('out.txt').write_text('\\n'.join(f'{i+1}: {l}' for i,l in enumerate(lines)) + '\\n')\n",
    "csv_select_col": "import pathlib\nrows=[l.split(',') for l in pathlib.Path('input.csv').read_text().splitlines()]\npathlib.Path('out.txt').write_text('\\n'.join(f'{r[0]},{r[2]}' for r in rows) + '\\n')\n",
    "replace_word": "import pathlib\nt=pathlib.Path('input.txt').read_text().replace('foo','bar')\npathlib.Path('out.txt').write_text(t)\n",
    "group_and_count": "import pathlib, collections\nc=collections.Counter(l.split()[0] for l in pathlib.Path('input.txt').read_text().splitlines() if l.strip())\nitems=sorted(c.items(), key=lambda kv:(-kv[1], kv[0]))\npathlib.Path('out.txt').write_text('\\n'.join(f'{k} {n}' for k,n in items) + '\\n')\n",
    "transpose_matrix": "import pathlib\nrows=[l.split() for l in pathlib.Path('input.txt').read_text().splitlines()]\nout=[' '.join(rows[r][c] for r in range(len(rows))) for c in range(len(rows[0]))]\npathlib.Path('out.txt').write_text('\\n'.join(out) + '\\n')\n",
    "dedupe_keep_last": "import pathlib\nlines=pathlib.Path('input.txt').read_text().splitlines()\nout=[]\nfor l in reversed(lines):\n    if l not in out: out.append(l)\nout.reverse()\npathlib.Path('out.txt').write_text('\\n'.join(out) + '\\n')\n",
    "parse_log_levels": "import pathlib, re\nt=pathlib.Path('input.txt').read_text()\nlevels=re.findall(r'\\[(\\w+)\\]', t)\npathlib.Path('out.txt').write_text('\\n'.join(levels) + '\\n')\n",
    "csv_join": "import pathlib\na={r.split(',')[0]: r.split(',')[1] for r in pathlib.Path('a.csv').read_text().splitlines()}\nb={r.split(',')[0]: r.split(',')[1] for r in pathlib.Path('b.csv').read_text().splitlines()}\nout=[f'{k},{v},{b.get(k, \"NA\")}' for k,v in a.items()]\npathlib.Path('out.txt').write_text('\\n'.join(out) + '\\n')\n",
}


# ── T5：CLI 实现 ──────────────────────────────────────────────────────────────

T5_SCRIPTS: dict[str, str] = {
    "echo_args": "import sys\nprint(' '.join(sys.argv[1:]))\n",
    "exit_code_on_empty": "import sys\nsys.exit(2 if len(sys.argv) < 2 else 0)\n",
    "sum_numbers": "import sys\nprint(sum(int(x) for x in sys.argv[2:]))\n",
    "count_lines": "import sys\nprint(len(open(sys.argv[2]).read().splitlines()))\n",
    "greet_flag": "import sys\nprint(f'Hello, {sys.argv[2]}!')\n",
    "word_count_file": "import sys\nt=open(sys.argv[2]).read()\nprint(len(t.splitlines()), len(t.split()))\n",
    "filter_lines": "import sys\npat, path = sys.argv[2], sys.argv[3]\nhits=[l for l in open(path).read().splitlines() if pat in l]\nprint('\\n'.join(hits))\nsys.exit(0 if hits else 1)\n",
    "csv_column": "import sys\nidx=int(sys.argv[2]); path=sys.argv[3]\nrows=open(path).read().splitlines()[1:]\nprint('\\n'.join(r.split(',')[idx-1] for r in rows))\n",
    "json_field": "import sys, json\nprint(json.load(open(sys.argv[3]))[sys.argv[2]])\n",
    "top_words": "import sys, collections\nn=int(sys.argv[2]); path=sys.argv[3]\nc=collections.Counter(open(path).read().split())\nfor w,_ in c.most_common(n): print(w)\n",
    "print_version": "import sys\nif '--version' in sys.argv: print('v1.0')\n",
    "upper_arg": "import sys\nprint(sys.argv[2].upper())\n",
    "repeat_flag": "import sys\nprint(sys.argv[3] * int(sys.argv[2]))\n",
    "file_exists_check": "import sys, os\nprint('yes' if os.path.exists(sys.argv[2]) else 'no')\n",
    "sum_stdin": "import sys\nprint(sum(int(x) for x in sys.stdin.read().split()))\n",
    "sort_unique_stdin": "import sys\nprint('\\n'.join(sorted(set(sys.stdin.read().split()))))\n",
    "extract_numbers": "import sys, re\nprint('\\n'.join(re.findall(r'\\d+', open(sys.argv[2]).read())))\n",
    "config_merge": "import sys, json\na=json.load(open(sys.argv[2])); b=json.load(open(sys.argv[3]))\na.update(b)\nprint(json.dumps(a))\n",
    "tail_n": "import sys\nn=int(sys.argv[2]); lines=open(sys.argv[3]).read().splitlines()\nprint('\\n'.join(lines[-n:]))\n",
    "csv_sum_column": "import sys\nidx=int(sys.argv[2]); rows=open(sys.argv[3]).read().splitlines()[1:]\nprint(sum(int(r.split(',')[idx-1]) for r in rows))\n",
}


def reference_for(task: Task) -> dict[str, str]:
    case = task.meta.get("case", "")
    if task.template == "t3_refactor":
        return t3_reference(task)
    if task.template == "t4_transform":
        return {"make.py": T4_SCRIPTS[case]}
    if task.template == "t5_cli":
        return {"cli.py": T5_SCRIPTS[case]}
    return {}


def verify(template: str | None) -> int:
    tasks = [t for t in generate_all() if template is None or t.template == template]
    failures = 0
    for task in tasks:
        ref = reference_for(task)
        if not ref:
            print(f"[skip] {task.task_id} 无参考解法")
            continue
        with tempfile.TemporaryDirectory(prefix=f"ref-{task.task_id}-") as tmp:
            ws = Path(tmp)
            task.materialize(ws)
            with Sandbox(ws, task_id=task.task_id, timeout_s=task.timeout_s) as box:
                for rel, content in ref.items():
                    box.write(rel, content)
                # T4 需要先跑 make.py 生成 out.txt
                if task.template == "t4_transform":
                    gen = box.run(["python", "make.py"])
                    if not gen.ok:
                        print(f"[FAIL] {task.task_id} 生成脚本失败: {gen.tail(300)}")
                        failures += 1
                        continue
                result = box.run(task.check_cmd)
                if result.ok:
                    print(f"[ ok ] {task.task_id} {task.template} {task.difficulty}")
                else:
                    print(f"[FAIL] {task.task_id} 参考解法未通过: {result.tail(300)}")
                    failures += 1
    return failures


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--template", default=None)
    args = parser.parse_args()
    failures = verify(args.template)
    if failures:
        print(f"\n{failures} 个失败")
        return 1
    print("\n参考解法全部通过 ✓")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
