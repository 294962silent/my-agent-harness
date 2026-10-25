"""LongMemEval 跑批公共组件：模型客户端、数据加载、历史渲染。

设计原则：
- 只依赖 OpenAI 兼容协议（本项目走 OpenCode 网关）；
- 渲染出的历史严格保持「日期 + 会话」结构，便于后续做时间推理；
- 所有 arm（full / truncate / rag / v2）共用同一套提问模板，保证可比。
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DATA_DIR = Path(__file__).resolve().parent / "data"
DEFAULT_MODEL = "deepseek-v4.1-flash"
_OPENCODE_SESSION = os.environ.get("OPENCODE_SESSION_ID") or str(uuid.uuid4())


@dataclass
class Credential:
    api_key: str
    base_url: str


def load_credential(provider: str = "openai") -> Credential:
    """从 ~/.my-pi-agent/auth.json 读取凭据（不打印密钥）。"""
    path = Path.home() / ".my-pi-agent" / "auth.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    profile = data["active_profiles"].get(provider) or "default"
    entry = data["providers"][provider][profile]
    return Credential(
        api_key=entry["key"],
        base_url=entry.get("base_url") or os.environ.get("OPENAI_BASE_URL", ""),
    )


def make_client(cred: Credential | None = None):
    """构造 OpenAI 兼容客户端，并注入 OpenCode 网关要求的身份头。"""
    from openai import OpenAI

    cred = cred or load_credential()
    headers = {}
    if "opencode.ai" in (cred.base_url or ""):
        headers = {
            "x-opencode-session": _OPENCODE_SESSION,
            "User-Agent": "longmemeval-eval/0.1",
        }
    return OpenAI(api_key=cred.api_key, base_url=cred.base_url, default_headers=headers or None)


def load_dataset(name: str = "oracle") -> list[dict[str, Any]]:
    files = {
        "oracle": "longmemeval_oracle.json",
        "s": "longmemeval_s_cleaned.json",
        "m": "longmemeval_m_cleaned.json",
    }
    path = DATA_DIR / files[name]
    if not path.exists():
        raise FileNotFoundError(f"缺少数据集 {path}，请先下载")
    return json.loads(path.read_text(encoding="utf-8"))


def render_sessions(record: dict[str, Any], max_sessions: int | None = None) -> str:
    """把 haystack 会话渲染成带时间戳的纯文本。"""
    dates = record["haystack_dates"]
    sessions = record["haystack_sessions"]
    ids = record["haystack_session_ids"]

    pairs = list(zip(dates, sessions, ids, strict=False))
    if max_sessions is not None:
        pairs = pairs[-max_sessions:]

    blocks: list[str] = []
    for date, session, sid in pairs:
        lines = [f"### Session {sid} — {date}"]
        for msg in session:
            role = msg.get("role", "user")
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
            lines.append(f"{role}: {content}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def build_answer_prompt(
    record: dict[str, Any], history: str, history_note: str = "", style: str = "default"
) -> list[dict[str, str]]:
    """统一的提问模板：历史 + 当前日期 + 问题。

    style="default"：原始模板（简洁、缺失即说不知道）。
    style="memory"：为"检索到的部分记忆"调过的模板，鼓励跨会话聚合与个性化。
    """
    if style == "memory":
        system = (
            "You are a helpful assistant with long-term memory of the user's past conversations.\n"
            "The context below is selectively retrieved from that history; it may be partial but "
            "usually contains the evidence needed.\n"
            "Answer using it, and follow these rules:\n"
            "- If the question is about the user's preferences or habits, tailor the answer to them.\n"
            "- If answering requires combining facts from different sessions, combine them.\n"
            "- If several values conflict, prefer the most recent one.\n"
            "- Keep the answer to 1-2 sentences.\n"
            "Only say you don't know if the evidence is truly absent."
        )
    else:
        system = (
            "You are a helpful assistant that remembers the user's past conversations.\n"
            "Below is the conversation history (chronological, with dates).\n"
            "Answer the final question using that history. Be concise: one short sentence.\n"
            "If the history does not contain the answer, say you don't know."
        )
    if history_note:
        system += f"\n\n{history_note}"
    user = f"{history}\n\nCurrent date: {record['question_date']}\n\nQuestion: {record['question']}"
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def truncate_tokens(text: str, max_tokens: int, chars_per_token: float = 3.5) -> str:
    """按 token 预算粗略截断（保留尾部，符合「最近优先」的朴素策略）。"""
    max_chars = int(max_tokens * chars_per_token)
    if len(text) <= max_chars:
        return text
    return "...(earlier history truncated)...\n" + text[-max_chars:]
