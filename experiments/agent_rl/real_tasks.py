"""把验证过的真实任务接入现有评测框架（适配 Task 接口）。

这一步完成 superagent 融合点 1 的闭环：
真实 PR（Issue + 测试改动）→ Task → 现有沙盒评测 → pass^k 指标。

与合成任务的区别：
- workspace 是一个**真实的开源仓库**（浅克隆的 base commit）；
- 判定是**PR 自己带来的测试**；
- 任务描述是**真实的 Issue 正文**（模糊、可能不完整）。

用法：
    uv run python -m experiments.agent_rl.real_tasks --list
    uv run python -m experiments.agent_rl.real_tasks --materialize <task_id> --dest ./ws
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

RESULTS = Path(__file__).resolve().parent / "results" / "real"
VERIFIED = RESULTS / "verified_tasks.jsonl"


@dataclass
class RealTask:
    """真实任务的 Task 兼容表示。"""

    task_id: str
    repo: str
    base_sha: str
    head_sha: str
    prompt: str
    test_files: list[str]
    changed_files: list[str]
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def check_cmd(self) -> list[str]:
        tests = " ".join(f'"{t}"' for t in self.test_files[:3])
        return ["sh", "-lc", f"python -m pytest -q {tests} --no-header -p no:cacheprovider"]

    def materialize(self, dest: Path) -> bool:
        """浅拉取 base commit 到 dest，再用 head 版本的测试覆盖。"""
        dest.mkdir(parents=True, exist_ok=True)
        steps = [
            ["git", "init", "-q"],
            ["git", "remote", "add", "origin", f"https://github.com/{self.repo}.git"],
            ["git", "fetch", "--depth", "1", "-q", "origin", self.base_sha],
            ["git", "checkout", "-q", "FETCH_HEAD"],
        ]
        for step in steps:
            proc = subprocess.run(step, cwd=dest, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                return False
        # 用 head 的测试文件覆盖
        for test_path in self.test_files:
            content = _fetch_file(self.repo, self.head_sha, test_path)
            if content is None:
                continue
            target = dest / test_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return True


def _fetch_file(repo: str, sha: str, path: str) -> str | None:
    url = f"https://raw.githubusercontent.com/{repo}/{sha}/{path}"
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    handlers = [urllib.request.ProxyHandler({"https": proxy, "http": proxy})] if proxy else []
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "harness"}), timeout=40) as r:
            return r.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def load_verified(path: Path = VERIFIED) -> list[RealTask]:
    if not path.exists():
        return []
    tasks = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        body = (raw.get("body") or "").strip()
        title = raw.get("title") or ""
        # 真实 Issue 的措辞通常模糊——这正是我们想要的难度
        prompt = f"{title}\n\n{body}".strip() if body else title
        prompt += "\n\n请修改代码使仓库的测试通过。不要修改测试文件。"
        tasks.append(
            RealTask(
                task_id=raw["task_id"],
                repo=raw["repo"],
                base_sha=raw["base_sha"],
                head_sha=raw["head_sha"],
                prompt=prompt[:4000],
                test_files=raw["test_files"],
                changed_files=raw["changed_files"],
                meta={**raw.get("meta", {}), "source": "real_pr"},
            )
        )
    return tasks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--materialize", default=None)
    parser.add_argument("--dest", default=None)
    args = parser.parse_args()

    tasks = load_verified()
    if not tasks:
        print(f"没有已验证的任务（{VERIFIED}）")
        return 1

    if args.list:
        print(f"真实任务: {len(tasks)}")
        by_repo: dict[str, int] = {}
        for task in tasks:
            by_repo[task.repo] = by_repo.get(task.repo, 0) + 1
        for repo, count in sorted(by_repo.items(), key=lambda kv: -kv[1]):
            print(f"  {repo:28s} {count}")
        print()
        for task in tasks[:5]:
            print(f"  {task.task_id}")
            print(f"    测试: {task.test_files[:2]}")
            print(f"    提示: {task.prompt[:80].replace(chr(10), ' ')}")
        return 0

    if args.materialize:
        task = next((t for t in tasks if t.task_id == args.materialize), None)
        if task is None:
            print(f"找不到任务 {args.materialize}")
            return 1
        dest = Path(args.dest) if args.dest else Path(tempfile.mkdtemp(prefix="real-task-"))
        ok = task.materialize(dest)
        print(f"{'OK' if ok else 'FAIL'} -> {dest}")
        print(f"判定命令: {task.check_cmd}")
        return 0 if ok else 1

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
