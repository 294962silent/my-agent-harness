"""子进程环境变量过滤测试：敏感凭据不泄漏，系统变量保留，/login 不写 os.environ。"""

from __future__ import annotations

import os

import pytest

from my_coding_agent.env_filter import filter_env, is_sensitive_env_key
from my_coding_agent.macro import MacroEngine
from my_coding_agent.tools.bash import make_bash_tool

SECRET_KEYS = [
    "DEEPSEEK_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "ANTIGRAVITY_ACCESS_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "GITHUB_TOKEN",
    "MY_API_KEY",
]
SAFE_KEYS = ["PATH", "HOME", "USERPROFILE", "SystemRoot", "LANG", "PI_MODEL", "MY_AGENT_HOME"]


def _echo_env(var_name: str) -> str:
    """跨平台读取环境变量的 shell 命令。"""
    return f"echo %{var_name}%" if os.name == "nt" else f'echo "${var_name}"'


def test_filter_env_removes_secrets_and_keeps_system_vars() -> None:
    source = {key: "secret-value" for key in SECRET_KEYS}
    source.update({key: "safe-value" for key in SAFE_KEYS})

    filtered = filter_env(source)

    assert set(filtered) == set(SAFE_KEYS)
    for key in SECRET_KEYS:
        assert is_sensitive_env_key(key), key
    for key in SAFE_KEYS:
        assert not is_sensitive_env_key(key), key
    # 不修改调用方传入的映射
    assert source["DEEPSEEK_API_KEY"] == "secret-value"


def test_filter_env_defaults_to_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-live-secret")
    monkeypatch.setenv("MY_TEST_SAFE_VAR", "visible")

    filtered = filter_env()

    assert "DEEPSEEK_API_KEY" not in filtered
    assert filtered["MY_TEST_SAFE_VAR"] == "visible"
    if "PATH" in os.environ:
        assert filtered["PATH"] == os.environ["PATH"]


def test_macro_shell_drops_api_keys_but_keeps_safe_vars(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-super-secret-leak")
    monkeypatch.setenv("MY_SAFE_VAR", "visible-value")
    engine = MacroEngine(workspace=tmp_path)

    safe_res = engine.execute_shell(_echo_env("MY_SAFE_VAR"), cwd=tmp_path)
    secret_res = engine.execute_shell(_echo_env("DEEPSEEK_API_KEY"), cwd=tmp_path)

    assert "visible-value" in safe_res["output"]
    assert "sk-super-secret-leak" not in secret_res["output"]


@pytest.mark.anyio
async def test_bash_tool_drops_api_keys(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-bash-secret-leak")
    tool = make_bash_tool(tmp_path)

    res = await tool.execute(command=_echo_env("DEEPSEEK_API_KEY"), timeout=30)

    assert "sk-bash-secret-leak" not in str(res)
