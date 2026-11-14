"""Best-effort memory redaction. Regex rules cannot recognize every secret format."""

from __future__ import annotations

import re

_REDACTED = "[REDACTED]"
_PRIVATE_KEY = re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----[\s\S]*?-----END (?:[A-Z0-9]+ )*PRIVATE KEY-----")
_PROVIDER_TOKEN = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,}|AIza[A-Za-z0-9_-]{25,}|xox[baprs]-[A-Za-z0-9-]{10,}|(?:AKIA|ASIA)[A-Z0-9]{16})(?![A-Za-z0-9])"
)
_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+")
# Match complete credential names, optionally prefixed by underscore/hyphen
# segments. Token budgets, max_tokens and secret_scanning_enabled are config,
# not credentials. Unknown naming conventions remain outside this heuristic.
_CREDENTIAL_NAME = r"(?:[a-z0-9]+[_-])*(?:api[_-]?key|(?:access|auth|refresh|id)[_-]?token|token|secret|clientsecret|password|passwd|credential|private[_-]?key|access[_-]?key(?:[_-]?id)?)"
_NAMED = re.compile(
    r"(?i)(?<![\w-])([\"']?" + _CREDENTIAL_NAME + r"[\"']?\s*[:=]\s*)(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;\r\n}]+)"
)
_SENSITIVE_KEY = re.compile(_CREDENTIAL_NAME, re.IGNORECASE)


def redact_memory_text(text: str) -> str:
    """Redact recognizable credentials while preserving ordinary text and ports.

    Call before model requests and persistence; this is a best-effort policy,
    not a guarantee that arbitrary encoded or unfamiliar secrets are detected.
    """
    text = _PRIVATE_KEY.sub(_REDACTED, text)
    text = _PROVIDER_TOKEN.sub(_REDACTED, text)
    text = _BEARER.sub(lambda match: match.group(1) + _REDACTED, text)
    return _NAMED.sub(lambda match: match.group(1) + _REDACTED, text)


def redact_memory_value(key: str, value: str) -> str:
    """A sensitive structured field also protects short, unrecognizable values."""
    return _REDACTED if _SENSITIVE_KEY.fullmatch(key.strip()) else redact_memory_text(value)
