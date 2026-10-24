"""子进程环境变量过滤：阻止 API Key / Token / Secret 泄漏给模型可执行的子进程。

模型可以驱动 shell 工具执行任意命令（如 `env`），若子进程完整继承父进程环境，
则父进程中的凭据会被直接读出。本模块提供统一的白名单式过滤函数：
剔除敏感键名，保留 PATH/HOME/USERPROFILE/SystemRoot 等系统运行必需变量。
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping

# 键名匹配（大小写不敏感）：任意位置的 _API_KEY/_TOKEN/_SECRET，或这些提供商前缀。
SENSITIVE_ENV_PATTERN = re.compile(
    r"(?i)(_API_KEY|_TOKEN|_SECRET|^ANTHROPIC_|^OPENAI_|^DEEPSEEK_|^GEMINI_|^GOOGLE_)"
)


def is_sensitive_env_key(key: str) -> bool:
    """判断环境变量名是否属于敏感凭据（大小写不敏感）。"""
    return SENSITIVE_ENV_PATTERN.search(key) is not None


def filter_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """复制一份环境变量并剔除敏感凭据；默认读取 os.environ。

    显式传入 env 时不会修改原映射，便于测试与复用。
    """
    source = os.environ if env is None else env
    return {key: value for key, value in source.items() if not is_sensitive_env_key(key)}


__all__ = ["SENSITIVE_ENV_PATTERN", "filter_env", "is_sensitive_env_key"]
