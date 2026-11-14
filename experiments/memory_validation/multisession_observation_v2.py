"""Fail-closed v2 readback for the first two ordinary extraction calls.

Each trace receives exactly one metadata/usage readback attempt. A missing or
ambiguous trace stops the campaign; no readback retry and no paid retry occur.
"""

from __future__ import annotations

import json
import os

from experiments.memory_validation.multisession_observation import (
    FirstExtractReadback,
    match_cloud,
)


class FirstExtractReadbackV2(FirstExtractReadback):
    """Require one successful cloud lookup for each of the first two calls."""

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
        found = [
            match_cloud(
                self.query(root),
                root,
                campaign=self.campaign,
                project=self.project,
                model=self.model,
                task_id=task_id,
                usage=usage,
            )
            for root, task_id, usage in self.calls
        ]
        if not all(found):
            raise RuntimeError("first two ordinary extraction traces unavailable; study stopped")
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
                    "readback_attempts_per_trace": 1,
                    "same_campaign_session": True,
                    "distinct_traces": True,
                    "rows": found,
                },
                stream,
                indent=2,
            )
            stream.flush()
            os.fsync(stream.fileno())
        self.verified = True
