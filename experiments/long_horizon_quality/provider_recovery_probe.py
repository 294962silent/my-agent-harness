"""Bounded, non-scoring provider recovery gate for the long-horizon study."""

from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import time

from experiments.benchmark_live.budget import (
    LIVE_BASE_URL,
    LIVE_MODEL,
    create_live_llm,
)
from experiments.long_horizon_code.public_pilot import install_live_credential
from my_agent_llm.models import Message


CLIENT_TIMEOUT_SECONDS = 90
ABSOLUTE_TIMEOUT_SECONDS = 300


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _safe_error(exc: BaseException) -> dict[str, str]:
    message = re.sub(
        r"(?i)(bearer|api[_ -]?key|token)\s*[:=]?\s*\S+",
        r"\1=<redacted>",
        str(exc),
    )
    message = re.sub(r"\s+", " ", message).strip()
    return {
        "type": type(exc).__name__,
        "message": message[:1000] or "<empty>",
    }


async def _run_attempt(index: int) -> dict:
    llm = create_live_llm(
        max_output_tokens=16,
        timeout=CLIENT_TIMEOUT_SECONDS,
    )
    started_at = _now()
    started = time.monotonic()
    try:
        response = await asyncio.wait_for(
            llm.achat(
                [
                    Message(
                        role="system",
                        content="This is an infrastructure health check. Do not call tools.",
                    ),
                    Message(
                        role="user",
                        content=f"Return exactly provider-ok. Check {index} of 3.",
                    ),
                ],
                max_tokens=16,
                temperature=0,
            ),
            timeout=ABSOLUTE_TIMEOUT_SECONDS,
        )
    except BaseException as exc:
        return {
            "attempt": index,
            "status": "failed",
            "started_at": started_at,
            "finished_at": _now(),
            "latency_seconds": round(time.monotonic() - started, 6),
            "error": _safe_error(exc),
        }
    elapsed = round(time.monotonic() - started, 6)
    content = response.content.strip()
    usage = response.usage.model_dump(mode="json") if response.usage else None
    valid = content.lower() == "provider-ok" and usage is not None
    return {
        "attempt": index,
        "status": "passed" if valid else "invalid_response",
        "started_at": started_at,
        "finished_at": _now(),
        "latency_seconds": elapsed,
        "finish_reason": response.finish_reason,
        "response_sha256": hashlib.sha256(content.encode()).hexdigest(),
        "response_chars": len(content),
        "usage": usage,
    }


async def run(attempts: int) -> dict:
    if attempts != 3:
        raise ValueError("provider recovery gate requires exactly three attempts")
    credential_source = install_live_credential()
    rows = []
    for index in range(1, attempts + 1):
        row = await _run_attempt(index)
        rows.append(row)
        if row["status"] != "passed":
            break
    return {
        "schema_version": 1,
        "protocol": "long-horizon-provider-recovery-v1",
        "scoring": False,
        "campaign_run_id": None,
        "credential_source": credential_source,
        "model": LIVE_MODEL,
        "endpoint": LIVE_BASE_URL,
        "client_timeout_seconds": CLIENT_TIMEOUT_SECONDS,
        "absolute_timeout_seconds": ABSOLUTE_TIMEOUT_SECONDS,
        "client_versions": {
            name: importlib.metadata.version(name)
            for name in ("httpx", "openai")
        },
        "attempts_requested": attempts,
        "attempts_completed": len(rows),
        "status": (
            "passed"
            if len(rows) == attempts
            and all(row["status"] == "passed" for row in rows)
            else "failed"
        ),
        "rows": rows,
        "credentials_persisted": False,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = asyncio.run(run(args.attempts))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "attempts_completed": result["attempts_completed"],
                "attempts_requested": result["attempts_requested"],
            },
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
