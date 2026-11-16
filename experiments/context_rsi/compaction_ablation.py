"""Zero-provider synthetic context mechanism comparison; not a quality benchmark."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace

from my_agent_core.artifacts import ArtifactStore
from my_agent_core.context import ContextManager, ContextSessionBridge, estimate_tokens
from my_agent_core.context_compaction import CompactionPolicy
from my_agent_core.session import Session
from my_agent_llm import Message


def encoded(messages):
    return json.dumps([m.model_dump() for m in messages], ensure_ascii=False, sort_keys=True).encode()


class SyntheticSummarizer:
    """A deterministic fixture response, never a provider client."""

    def __init__(self):
        self.calls = 0
        self.config = SimpleNamespace(max_tokens=512)

    async def achat(self, **kwargs):
        assert kwargs.get("tools") == []
        assert kwargs.get("max_tokens", self.config.max_tokens) == 512
        self.calls += 1
        return SimpleNamespace(
            content="<summary>Goals: synthetic inspection. Constraints: preserve source. Decisions: prior steps inspected. Changed files: none. Validation: fixture only. Pending work: final step. Artifact references: none.</summary>",
            usage=None,
            model="synthetic-not-a-provider",
        )


def fixture():
    messages = [
        Message(role="system", content="Synthetic mechanism fixture."),
        Message(role="user", content="Inspect synthetic outputs; do not change canonical history."),
    ]
    for index in range(8):
        messages.extend(
            [
                Message(role="user", content=f"Inspect stage {index}."),
                Message(
                    role="assistant",
                    content="",
                    metadata={"tool_calls": [{"id": f"call-{index}", "name": "fixture_tool", "arguments": {}}]},
                ),
                Message(
                    role="tool",
                    content=json.dumps({"exit_code": 0, "stdout": f"NONCE-{index}-" + "synthetic-data " * 220}),
                    metadata={"tool_name": "fixture_tool", "tool_call_id": f"call-{index}"},
                ),
                Message(role="assistant", content=f"Stage {index} inspected."),
            ]
        )
    messages.append(Message(role="user", content="Continue with the final stage."))
    return messages


async def run(output):
    output = Path(output)
    if output.exists():
        raise FileExistsError("mechanism output must be a new directory")
    canonical = fixture()
    original = encoded(canonical)
    assert all(len(m.content) < 6000 for m in canonical if m.role == "tool")
    # Identical declared fixture schema for both arms; no provider receives it.
    schema = [
        {"type": "function", "function": {"name": "fixture_tool", "parameters": {"type": "object", "properties": {}}}}
    ]
    result = {
        "kind": "synthetic_context_mechanism_only",
        "paid_model_requests": 0,
        "fixture_sha256": hashlib.sha256(original).hexdigest(),
        "canonical_bytes": len(original),
        "canonical_estimated_tokens": estimate_tokens(canonical),
        "context_budget": 5000,
        "output_reserve": 512,
        "safety_margin": 256,
        "summary_max_tokens": 512,
        "declared_fixture_schema_sha256": hashlib.sha256(json.dumps(schema, sort_keys=True).encode()).hexdigest(),
        "cache_read_tokens": None,
        "cache_write_tokens": None,
        "arms": {},
    }
    with tempfile.TemporaryDirectory(prefix="context-mechanism-") as temporary:
        for mode in ("baseline", "layered"):
            llm = SyntheticSummarizer()
            store = ArtifactStore(Path(temporary) / mode)
            policy = CompactionPolicy(
                mode=mode,
                model_context_window=10000,
                output_reserve=512,
                safety_margin=256,
                summary_max_tokens=512,
                recent_tool_groups=2,
            )
            ctx = ContextManager(
                budget=5000, keep_recent_tokens=750, llm=llm, artifact_store=store, compaction_policy=policy
            )
            view = await ctx.prepare(canonical)
            calls = llm.calls
            report = dict(ctx.compaction_report)
            repeated = await ctx.prepare(canonical)
            checks = 0
            for message in view:
                meta = message.metadata or {}
                if meta.get("micro_projection"):
                    original_tool = next(
                        m
                        for m in canonical
                        if m.role == "tool" and (m.metadata or {}).get("tool_call_id") == meta["tool_call_id"]
                    )
                    text = store.read_page(meta["artifact_id"])["text"]
                    assert text == original_tool.content and "NONCE-" in text
                    checks += 1
            assert encoded(canonical) == original
            assert encoded(view) == encoded(repeated)
            assert llm.calls == calls
            result["arms"][mode] = {
                "sent_bytes": len(encoded(view)),
                "sent_estimated_tokens": estimate_tokens(view),
                "synthetic_summary_calls": calls,
                "readback_checks": checks,
                "readback_integrity": True if checks else None,
                "canonical_unchanged": True,
                "repeat_stable": True,
                "capability_profile": policy.capability_profile(),
                "report": report,
            }
        assert result["arms"]["baseline"]["synthetic_summary_calls"] == 1
        assert result["arms"]["layered"]["synthetic_summary_calls"] == 0
        assert result["arms"]["layered"]["readback_checks"] > 0
        session = Session(path=Path(temporary) / "fold-session.jsonl")
        source = canonical[:6] + [Message(role="user", content="Next stage after evaluated attempt.")]
        for message in source:
            session.add_message(message.role, message.content, **(message.metadata or {}))
        source = session.get_current_path_messages()
        llm = SyntheticSummarizer()
        policy = CompactionPolicy(mode="layered")
        ctx = ContextManager(
            budget=10000, llm=llm, artifact_store=ArtifactStore(Path(temporary) / "fold"), compaction_policy=policy
        )
        ctx.register_completed_prefix(
            source,
            6,
            "Goals: synthetic inspection. Constraints: preserve source. Decisions: one attempt evaluated, not task success. Changed files: none. Validation: synthetic fixture. Pending work: next stage. Artifact references: none.",
        )
        folded = await ctx.prepare(source)
        assert ctx.compaction_report["phase_fold"] is True
        ContextSessionBridge(session).write_compaction(ctx)
        restored = ContextManager(budget=10000, llm=llm, artifact_store=ctx.artifact_store, compaction_policy=policy)
        loaded = Session.load(session.path)
        ContextSessionBridge(loaded).restore_cache(restored)
        assert encoded(await restored.prepare(source)) == encoded(folded)
        assert encoded(loaded.get_current_path_messages()) == encoded(source)
        result["explicit_prefix_fold"] = {
            "summary_source": "host_static_fixture_not_llm",
            "synthetic_summary_calls": llm.calls,
            "coverage_fingerprints_validated": bool(ctx.cache_identity),
            "canonical_unchanged": True,
            "session_resume_stable": True,
        }
    output.mkdir(parents=True)
    data = json.dumps(result, indent=2).encode()
    (output / "metrics.json").write_bytes(data)
    (output / "REPORT.md").write_text(
        "# Synthetic local compaction mechanism\n\nZero paid/provider calls. Identical canonical fixture, context budget, declared fixture tool schema and output reserves. Baseline uses one deterministic fake summary; layered first externalizes older complete successful tool groups, verifies bounded read-back and avoids that fake call. These are byte/token estimates and mechanism contracts, not real answer quality, provider KV-cache hits or performance benefits. Cache fields remain unavailable. The separate prefix-fold summary is explicitly host-supplied static text; canonical Session content survives save/load. Metrics contain only synthetic audit data.\n",
        encoding="utf-8",
    )
    (output / "manifest.json").write_text(
        json.dumps(
            {"files": [{"path": "metrics.json", "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}]},
            indent=2,
        )
    )
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(run(args.output))
    print(
        json.dumps(
            {
                "paid_model_requests": result["paid_model_requests"],
                "arms": {
                    k: {
                        f: v[f]
                        for f in ("sent_bytes", "sent_estimated_tokens", "synthetic_summary_calls", "readback_checks")
                    }
                    for k, v in result["arms"].items()
                },
            }
        )
    )
