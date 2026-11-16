"""Mandatory first two legitimate extraction traces, metadata/usage only."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.parse
import urllib.request


def _object(value):
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("remote metadata object differs")


def match_cloud(rows, root, *, campaign, project, model, task_id, usage):
    trace = hashlib.sha256(root.encode()).hexdigest()[:32]
    for row in rows:
        try:
            meta = _object(row.get("metadata"))
            # The observed CLI/API may already decode parent_id; do not json.loads(dict).
            if meta.get("parent_id") is not None:
                _object(meta["parent_id"])
            wanted = {
                "root_id": root,
                "campaign": campaign,
                "arm": "shared_facts",
                "stage": "extract",
                "task_id": task_id,
                "status": "ok",
            }
            if (
                any(meta.get(k) != v for k, v in wanted.items())
                or row.get("projectId") != project
                or row.get("traceId") != trace
                or row.get("sessionId") != campaign
                or str(row.get("type", "")).upper() != "GENERATION"
                or not row.get("endTime")
                or row.get("model") != model
                or (row.get("usageDetails") or {}).get("total") != usage.get("total_tokens")
            ):
                continue
            return {
                "trace_id": trace,
                "session_id": campaign,
                "project_id": project,
                "root_id": root,
                "task_id": task_id,
                "model": model,
                "usage_details": {k: v for k, v in row["usageDetails"].items() if k in ("input", "output", "total")},
                "metadata_only": True,
            }
        except (ValueError, TypeError):
            continue
    return None


def query_cloud(root, base="https://cloud.langfuse.com"):
    public, secret = os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")
    if not public or not secret or base.rstrip("/") != "https://cloud.langfuse.com":
        raise RuntimeError("required cloud readback configuration missing")
    trace = hashlib.sha256(root.encode()).hexdigest()[:32]
    params = urllib.parse.urlencode(
        {"traceId": trace, "fields": "core,basic,model,usage,metadata", "expandMetadata": "parent_id", "limit": 100}
    )
    request = urllib.request.Request(
        base.rstrip("/") + "/api/public/v2/observations?" + params,
        headers={"Authorization": "Basic " + base64.b64encode((public + ":" + secret).encode()).decode()},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError("cloud readback HTTP status differs")
            value = json.loads(response.read(2 * 1024 * 1024))
        return value.get("data", [])
    except BaseException:
        raise RuntimeError("required metadata cloud readback failed") from None


class FirstExtractReadback:
    def __init__(self, owner, *, campaign, project, model, receipt, query=query_cloud, wait=time.sleep):
        self.owner, self.campaign, self.project, self.model = owner, campaign, project, model
        self.receipt, self.query, self.wait = Path(receipt), query, wait
        self.calls, self.verified = [], False
        if self.receipt.exists():
            prior = json.loads(self.receipt.read_text(encoding="utf-8"))
            rows = prior.get("rows")
            if (
                prior.get("status") != "verified"
                or prior.get("campaign") != campaign
                or prior.get("project") != project
                or prior.get("model") != model
                or prior.get("sample_count") != 2
                or not isinstance(rows, list)
                or len(rows) != 2
                or len({row.get("trace_id") for row in rows}) != 2
                or any(
                    row.get("session_id") != campaign or row.get("project_id") != project or row.get("model") != model
                    for row in rows
                )
            ):
                raise RuntimeError("existing first-extraction readback receipt differs")
            self.verified = True

    def __call__(self, root, response, *, task_id):
        if self.verified:
            return
        if len(self.calls) >= 2:
            raise RuntimeError("first extraction readback not verified; no later dispatch")
        self.calls.append((root, task_id, dict(response.usage or {})))
        if len(self.calls) < 2:
            return
        if self.calls[0][0] == self.calls[1][0]:
            raise RuntimeError("first extraction traces must be distinct")
        self.owner.flush_and_check()
        for attempt in range(3):
            found = [
                match_cloud(
                    self.query(r), r, campaign=self.campaign, project=self.project, model=self.model, task_id=t, usage=u
                )
                for r, t, u in self.calls
            ]
            if all(found):
                self.receipt.parent.mkdir(parents=True, exist_ok=True)
                with self.receipt.open("x", encoding="utf-8") as stream:
                    json.dump(
                        {
                            "status": "verified",
                            "campaign": self.campaign,
                            "project": self.project,
                            "model": self.model,
                            "sample_count": 2,
                            "extra_model_requests": 0,
                            "same_campaign_session": True,
                            "distinct_traces": True,
                            "rows": found,
                        },
                        stream,
                        indent=2,
                    )
                self.verified = True
                return
            if attempt < 2:
                self.wait(2)
        raise RuntimeError("first two ordinary extraction traces unavailable; study stopped")
