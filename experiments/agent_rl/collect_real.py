"""从真实开源仓库采集「Issue → PR → 测试」三元组，转成可评测的 Task。

这是 superagent 融合点 1 的实现：superagent 监听 GitHub Issue 并产出 PR，
本脚本把**同样形态的真实工作**变成可自动判定的评测任务。

采集流程：
1. 找一个仓库的**已合并 PR**，且该 PR 关联了 Issue（PR 正文里出现 "fixes #123" 等）；
2. 取 PR 的 base commit 作为「修改前」状态；
3. 取 PR 修改的测试文件，作为判定依据；
4. 组装成 Task：workspace = 该 commit 的代码快照，check = 跑那些测试。

难点与应对：
- 仓库太大：只保留与 PR 改动相关的文件 + 依赖，或整仓浅克隆（本机可承受 8MB 以内）；
- 测试依赖：默认用仓库自己的 pytest，容器里 pip install -e . 可能失败 → 记为任务不可用并跳过；
- 判定不稳：必须验证「修改前测试失败、修改后测试通过」，否则丢弃。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

GITHUB_API = "https://api.github.com"
OUT_DIR = Path(__file__).resolve().parent / "results" / "real"

_LINK_RE = re.compile(r"(?:fix(?:e[sd])?|close[sd]?|resolve[sd]?)\s+#(\d+)", re.I)


@dataclass
class RealTask:
    """一个真实任务。"""

    task_id: str
    repo: str
    issue_number: int
    pr_number: int
    base_sha: str
    head_sha: str
    title: str
    body: str
    test_files: list[str] = field(default_factory=list)
    changed_files: list[str] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "repo": self.repo,
            "issue_number": self.issue_number,
            "pr_number": self.pr_number,
            "base_sha": self.base_sha,
            "head_sha": self.head_sha,
            "title": self.title,
            "body": self.body,
            "test_files": self.test_files,
            "changed_files": self.changed_files,
            "meta": self.meta,
        }


def _gh(path: str, token: str | None = None) -> Any:
    """调 GitHub API。用 curl 走系统代理，避免 python 的证书问题。"""
    import urllib.request

    req = urllib.request.Request(f"{GITHUB_API}{path}")
    req.add_header("User-Agent", "my-agent-harness")
    req.add_header("Accept", "application/vnd.github+json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    if proxy:
        handler = urllib.request.ProxyHandler({"https": proxy, "http": proxy})
        opener = urllib.request.build_opener(handler)
    else:
        opener = urllib.request.build_opener()
    with opener.open(req, timeout=40) as response:
        return json.loads(response.read().decode("utf-8"))


def find_issue_prs(repo: str, limit: int = 30, token: str | None = None) -> list[dict[str, Any]]:
    """找已合并且关联了 Issue 的 PR。"""
    prs = _gh(f"/repos/{repo}/pulls?state=closed&sort=updated&direction=desc&per_page={limit}", token)
    found = []
    for pr in prs:
        if not pr.get("merged_at"):
            continue
        body = pr.get("body") or ""
        match = _LINK_RE.search(body)
        # 没有关联 Issue 也接受：PR 标题 + 正文就是任务描述（真实工作中很常见）
        found.append(
            {
                "pr_number": pr["number"],
                "title": pr["title"],
                "body": body,
                "issue_number": int(match.group(1)) if match else 0,
                "base_sha": pr["base"]["sha"],
                "head_sha": pr["head"]["sha"],
                "merge_commit": pr.get("merge_commit_sha"),
            }
        )
    return found


def pr_files(repo: str, pr_number: int, token: str | None = None) -> list[dict[str, Any]]:
    return _gh(f"/repos/{repo}/pulls/{pr_number}/files?per_page=100", token)


def is_code_file(name: str) -> bool:
    """只接受 Python 源码/测试，排除 CI 配置与文档。"""
    if not name.endswith(".py"):
        return False
    if name.startswith(".github/"):
        return False
    return True


def is_test_file(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered.startswith("test")
        or "/test" in lowered
        or lowered.endswith("_test.py")
        or "conftest" in lowered
    )


def collect(repo: str, limit: int, token: str | None = None) -> list[RealTask]:
    """采集一个仓库的真实任务。"""
    candidates = find_issue_prs(repo, limit=limit, token=token)
    tasks: list[RealTask] = []
    for cand in candidates:
        files = pr_files(repo, cand["pr_number"], token)
        tests = [
            f["filename"]
            for f in files
            if is_code_file(f["filename"]) and is_test_file(f["filename"]) and f["status"] != "removed"
        ]
        source = [
            f["filename"]
            for f in files
            if is_code_file(f["filename"]) and not is_test_file(f["filename"]) and f["status"] != "removed"
        ]
        if not tests or not source:
            continue  # 没有测试改动，无法自动判定
        if len(files) > 12 or len(source) > 6:
            continue  # 太大的 PR 不适合当单次任务
        # 排除无意义的 PR：合并分支、发版本、纯文档
        title_lower = (cand["title"] or "").lower()
        if any(k in title_lower for k in ("merge stable", "merge main", "merge master", "bump version", "release ")):
            continue
        if not cand["title"] or len(cand["title"].strip()) < 8:
            continue

        issue: dict = {}
        if cand["issue_number"]:
            try:
                issue = _gh(f"/repos/{repo}/issues/{cand['issue_number']}", token)
            except Exception:  # noqa: BLE001
                pass

        tasks.append(
            RealTask(
                task_id=f"{repo.replace('/', '__')}-pr{cand['pr_number']}",
                repo=repo,
                issue_number=cand["issue_number"],
                pr_number=cand["pr_number"],
                base_sha=cand["base_sha"],
                head_sha=cand["head_sha"],
                title=issue.get("title") or cand["title"],
                body=(issue.get("body") or cand["body"] or "")[:3000],
                test_files=tests,
                changed_files=source,
                meta={
                    "additions": sum(f.get("additions", 0) for f in files),
                    "deletions": sum(f.get("deletions", 0) for f in files),
                    "num_files": len(files),
                },
            )
        )
    return tasks


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repos", required=True, help="逗号分隔，如 psf/requests,pallets/click")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--out", default=str(OUT_DIR / "real_tasks.jsonl"))
    args = parser.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    all_tasks: list[RealTask] = []
    for repo in [r.strip() for r in args.repos.split(",") if r.strip()]:
        try:
            tasks = collect(repo, args.limit, token)
            print(f"{repo}: {len(tasks)} 个可用任务")
            for task in tasks[:3]:
                print(f"   #{task.pr_number} tests={task.test_files[:2]} src={task.changed_files[:2]}")
            all_tasks.extend(tasks)
        except Exception as exc:  # noqa: BLE001
            print(f"{repo}: 失败 {type(exc).__name__}: {exc}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for task in all_tasks:
            fh.write(json.dumps(task.to_dict(), ensure_ascii=False) + "\n")
    print(f"\n合计 {len(all_tasks)} 个任务 -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
