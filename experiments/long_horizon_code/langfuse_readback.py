"""Metadata-only Langfuse readback for one experiment session."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path

import httpx


def _usage(row):
    value = row.get("usageDetails") or {}
    result = {}
    for key in ("input", "output", "total"):
        try:
            result[key] = int(value[key])
        except (KeyError, TypeError, ValueError):
            continue
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--from-start", required=True)
    parser.add_argument("--to-start", required=True)
    parser.add_argument("--base-url", default="https://cloud.langfuse.com")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    public = os.environ.get("LANGFUSE_PUBLIC_KEY")
    secret = os.environ.get("LANGFUSE_SECRET_KEY")
    if not public or not secret:
        raise SystemExit("Langfuse credentials are unavailable")
    url = args.base_url.rstrip("/") + "/api/public/v2/observations"
    response = httpx.get(
        url,
        params={
            "fields": "core,basic,usage",
            "fromStartTime": args.from_start,
            "toStartTime": args.to_start,
            "sessionId": args.session_id,
            "limit": 1000,
        },
        auth=(public, secret),
        timeout=30,
    )
    response.raise_for_status()
    rows = response.json().get("data", [])
    traces = {}
    for row in rows:
        trace_id = row.get("traceId")
        if not isinstance(trace_id, str):
            continue
        state = traces.setdefault(trace_id, {"observations": 0, "types": Counter(), "usage": Counter()})
        state["observations"] += 1
        state["types"][str(row.get("type"))] += 1
        for key, value in _usage(row).items():
            state["usage"][key] += value
    result = {
        "status": "ok",
        "session_id": args.session_id,
        "trace_count": len(traces),
        "observation_count": len(rows),
        "types": dict(Counter(str(row.get("type")) for row in rows)),
        "traces": {
            trace_id: {
                "observations": state["observations"],
                "types": dict(state["types"]),
                "usage": dict(state["usage"]),
            }
            for trace_id, state in sorted(traces.items())
        },
        "credentials_persisted": False,
    }
    payload = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")


if __name__ == "__main__":
    main()
