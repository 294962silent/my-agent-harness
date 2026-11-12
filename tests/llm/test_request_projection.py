# pyright: reportArgumentType=false
"""Request projection diagnostics: stable summaries, classifications, no secrets."""

from __future__ import annotations

import json
from types import SimpleNamespace

from my_agent_llm import Config, Message
from my_agent_llm.providers.anthropic import AnthropicProvider
from my_agent_llm.providers.openai import OpenAIProvider
from my_agent_llm.request_projection import RequestProjection


def test_classifications_and_prefix_logic():
    p = RequestProjection("t")
    m1 = [{"role": "user", "content": "a"}]
    assert p.observe(model="m", system=None, tools=None, messages=m1).classification == "initial"
    assert p.observe(model="m", system=None, tools=None, messages=m1).classification == "unchanged"
    assert p.observe(model="m", system=None, tools=None, messages=m1 + [{"role": "assistant", "content": "b"}]).classification == "append_only"
    assert p.observe(model="m2", system=None, tools=None, messages=m1).classification == "model_changed"
    assert p.observe(model="m2", system=None, tools=[{"name": "x"}], messages=m1).classification == "tools_changed"
    assert p.observe(model="m2", system="S", tools=[{"name": "x"}], messages=m1).classification == "system_changed"
    rewritten = p.observe(model="m2", system="S", tools=[{"name": "x"}], messages=[{"role": "system", "content": "z"}])
    assert rewritten.classification == "messages_rewritten"


def test_markers_explain_rewrites():
    p = RequestProjection("t")
    p.observe(model="m", system=None, tools=None, messages=[{"role": "user", "content": "a"}])
    p.mark_message_rewrite("branch_summary")
    p.mark_tool_preview("grep", "old output")
    rec = p.observe(model="m", system=None, tools=None, messages=[{"role": "user", "content": "b"}])
    assert rec.classification == "messages_rewritten"
    assert "messages:branch_summary" in rec.reasons
    assert "tool_preview:grep:old output" in rec.reasons


def test_bounded_history():
    p = RequestProjection("t", capacity=3)
    for i in range(5):
        p.observe(model="m", system=None, tools=None, messages=[{"role": "user", "content": str(i)}])
    assert len(p.history()) == 3
    assert p.last.seq == 5


def _openai_fake():
    calls = []

    class Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok", tool_calls=None), finish_reason="stop")],
                model=kwargs.get("model"),
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
            )

    return SimpleNamespace(chat=SimpleNamespace(completions=Completions())), calls


def test_openai_provider_conversion_is_stable_and_private():
    fake, calls = _openai_fake()
    provider = OpenAIProvider(Config(api_key="sk-secret", model="m"), client=fake)
    msgs = [Message(role="user", content="secret prompt body")]
    provider.chat(msgs, model="m")
    provider.chat(msgs, model="m")
    assert provider.request_projection.last.classification == "unchanged"
    assert calls[0]["messages"] == calls[1]["messages"]
    blob = json.dumps([r.as_dict() for r in provider.request_projection.history()], ensure_ascii=False)
    assert "secret prompt body" not in blob and "sk-secret" not in blob


def test_anthropic_provider_records_system_and_tools():
    class Messages:
        def create(self, **kwargs):
            return SimpleNamespace(
                content=[SimpleNamespace(type="text", text="ok")],
                model=kwargs.get("model"),
                stop_reason="end_turn",
                usage=SimpleNamespace(input_tokens=3, output_tokens=1),
            )

    provider = AnthropicProvider(
        Config(api_key="k", model="claude"), client=SimpleNamespace(messages=Messages())
    )
    provider.chat([Message(role="system", content="secret system"), Message(role="user", content="hi")], model="claude")
    rec = provider.request_projection.last
    assert rec.system_hash and rec.classification == "initial"
    blob = json.dumps(rec.as_dict(), ensure_ascii=False)
    assert "secret system" not in blob


def test_options_fingerprints_and_mutable_input_do_not_retain_prompt():
    recorder = RequestProjection("t")
    messages = [{"role": "user", "content": [{"text": "private nested prompt"}]}]
    recorder.observe(model="m", system=None, tools=None, messages=messages, options={"reasoning_effort": "low", "tool_choice": "auto"})
    changed = recorder.observe(model="m", system=None, tools=None, messages=messages, options={"reasoning_effort": "high", "tool_choice": "auto"})
    assert changed.classification == "request_config_changed"
    messages[0]["content"][0]["text"] = "mutated"
    messages.append({"role": "assistant", "content": "result"})
    assert recorder.observe(model="m", system=None, tools=None, messages=messages, options={"reasoning_effort": "high", "tool_choice": "auto"}).classification == "messages_rewritten"
    assert "private nested prompt" not in repr(vars(recorder))
    assert "mutated" not in repr(vars(recorder))


def test_provider_options_are_observed_without_changing_payload():
    fake, calls = _openai_fake()
    provider = OpenAIProvider(Config(api_key="secret", model="m"), client=fake)
    provider.chat([Message(role="user", content="body")], model="m", reasoning_effort="low", tool_choice="auto")
    provider.chat([Message(role="user", content="body")], model="m", reasoning_effort="low", tool_choice="none")
    assert provider.request_projection.last.classification == "request_config_changed"
    assert calls[-1]["tool_choice"] == "none"
    assert calls[-1]["reasoning_effort"] == "low"
