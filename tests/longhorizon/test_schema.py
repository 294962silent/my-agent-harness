from __future__ import annotations

import json

import pytest

from my_coding_agent.longhorizon.schema import (
    AuditReport,
    ManagerDecision,
    RoundRecord,
    SchemaError,
    parse_audit_report,
    parse_manager_decision,
    parse_role_episode,
    parse_round_record,
)


def test_parse_manager_decision_accepts_plain_and_fenced_json() -> None:
    plain = {
        "next": "execute",
        "reason": "one bounded change remains",
        "subtask": "update answer.py",
        "acceptance_criteria": ["python answer.py prints 42"],
        "related_rounds": [1, 2],
    }
    assert parse_manager_decision(json.dumps(plain)) == ManagerDecision(
        next="execute",
        reason="one bounded change remains",
        subtask="update answer.py",
        acceptance_criteria=("python answer.py prints 42",),
        related_rounds=(1, 2),
    )
    fenced = "```json\n" + json.dumps(plain) + "\n```"
    assert parse_manager_decision(fenced) == parse_manager_decision(json.dumps(plain))


def test_parse_audit_report_accepts_fenced_json_and_freezes_tuple_fields() -> None:
    payload = {
        "status": "complete",
        "summary": "verified",
        "evidence": ["answer.py:1"],
        "verified_facts": ["output is 42"],
        "remaining": [],
    }
    report = parse_audit_report("```json\n" + json.dumps(payload) + "\n```")
    assert report == AuditReport(
        status="complete",
        summary="verified",
        evidence=("answer.py:1",),
        verified_facts=("output is 42",),
        remaining=(),
    )


@pytest.mark.parametrize(
    ("prefix", "suffix"),
    [
        ("Auditor explanation:\n", ""),
        ("", "\nAuditor explanation after the block."),
        (
            "Short summary before.\n\n",
            "\n\nShort summary after.",
        ),
    ],
)
def test_parse_manager_decision_accepts_one_fenced_json_with_prose(
    prefix: str,
    suffix: str,
) -> None:
    payload = {
        "next": "execute",
        "reason": "one bounded change remains",
        "subtask": "update answer.py",
        "acceptance_criteria": ["python answer.py prints 42"],
    }
    fenced = "```json\n" + json.dumps(payload) + "\n```"

    decision = parse_manager_decision(prefix + fenced + suffix)

    assert decision.next == "execute"
    assert decision.subtask == "update answer.py"


def test_parse_audit_report_accepts_one_bare_json_with_prose() -> None:
    payload = {
        "status": "complete",
        "summary": "verified",
        "evidence": ['git status => "clean"', "nested syntax: {}"],
        "verified_facts": ["the tree is clean"],
        "remaining": [],
    }
    text = "I have enough verified evidence. Final audit JSON:\n\n" + json.dumps(payload)

    report = parse_audit_report(text)

    assert report.status == "complete"
    assert report.evidence == ('git status => "clean"', "nested syntax: {}")
    assert report.remaining == ()


def test_parse_manager_decision_rejects_multiple_bare_json_objects() -> None:
    first = json.dumps({"next": "done", "reason": "first"})
    second = json.dumps({"next": "done", "reason": "second"})

    with pytest.raises(SchemaError, match="ambiguous JSON objects"):
        parse_manager_decision(f"First: {first}\nSecond: {second}")


def test_parse_manager_decision_rejects_multiple_json_fences_as_ambiguous() -> None:
    first = '```json\n{"next":"done","reason":"first"}\n```'
    second = '```json\n{"next":"done","reason":"second"}\n```'
    text = f"Before.\n{first}\nBetween.\n{second}\nAfter."

    with pytest.raises(SchemaError, match="ambiguous JSON code blocks"):
        parse_manager_decision(text)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        (
            '{"next":"done","reason":"x","reason":"y"}',
            "duplicate JSON key",
        ),
        (
            '{"next":"done","reason":"x","related_rounds":[NaN]}',
            "non-finite JSON number",
        ),
        (
            '{"next":"done","reason":"x","unexpected":true}',
            "forbidden fields",
        ),
    ],
)
def test_single_json_fence_preserves_json_and_schema_strictness(
    body: str,
    message: str,
) -> None:
    text = f"Explanation.\n```json\n{body}\n```\nDone."

    with pytest.raises(SchemaError, match=message):
        parse_manager_decision(text)


def test_complete_audit_rejects_nonempty_remaining() -> None:
    payload = {
        "status": "complete",
        "summary": "verified",
        "evidence": ["answer.py:1"],
        "remaining": ["informational note"],
    }
    with pytest.raises(
        SchemaError,
        match="audit.remaining must be empty when audit.status is complete",
    ):
        parse_audit_report(json.dumps(payload))
    with pytest.raises(
        SchemaError,
        match="audit.remaining must be empty when audit.status is complete",
    ):
        AuditReport(
            status="complete",
            summary="verified",
            evidence=("answer.py:1",),
            remaining=("informational note",),
        )


@pytest.mark.parametrize("status", ["incomplete", "blocked"])
def test_noncomplete_audit_allows_nonempty_remaining(status: str) -> None:
    report = parse_audit_report(
        json.dumps(
            {
                "status": status,
                "summary": status,
                "evidence": ["answer.py:1"],
                "remaining": ["run another check"],
            }
        )
    )
    assert report.remaining == ("run another check",)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "not json",
        '{"next":"execute","reason":"x","unknown":1}',
        '{"next":"execute"}',
        '{"next":"rewind","reason":"x"}',
        '{"next":"execute","reason":"x","subtask":""}',
        '{"next":"ask","reason":"x","question":""}',
        '{"next":"execute","reason":"x","subtask":"s","acceptance_criteria":[]}',
        '{"next":"execute","reason":"x","subtask":"s","acceptance_criteria":["a"],"related_rounds":[0]}',
        '{"next":"execute","reason":"x","subtask":"s","acceptance_criteria":["a"],"next":"done"}',
        '```text\n{"next":"done","reason":"x"}\n```',
    ],
)
def test_parse_manager_decision_rejects_non_strict_output(text: str) -> None:
    with pytest.raises(SchemaError):
        parse_manager_decision(text)


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "complete", "summary": "x", "evidence": []},
        {"status": "done", "summary": "x", "evidence": ["e"]},
        {"status": "complete", "summary": "", "evidence": ["e"]},
        {"status": "complete", "summary": "x", "evidence": ["e"], "extra": True},
        {"status": "complete", "summary": "x"},
    ],
)
def test_parse_audit_report_rejects_invalid_contract(payload: dict) -> None:
    with pytest.raises(SchemaError):
        parse_audit_report(json.dumps(payload))


def test_role_episode_and_round_record_are_strict() -> None:
    episode_data = {
        "output": "done",
        "terminal": "end_turn",
        "iterations": 2,
        "tool_names": ["read", "write"],
        "usage": [{"prompt_tokens": 3, "completion_tokens": 2}],
        "session_path": "sessions/executor.jsonl",
        "cleanup_ok": True,
    }
    episode = parse_role_episode(json.dumps(episode_data))
    assert episode.tool_names == ("read", "write")
    assert episode.usage == ({"prompt_tokens": 3, "completion_tokens": 2},)
    with pytest.raises(SchemaError):
        parse_role_episode(json.dumps({**episode_data, "iterations": True}))
    with pytest.raises(SchemaError):
        parse_role_episode(json.dumps({**episode_data, "unexpected": 1}))

    decision = ManagerDecision(
        next="execute",
        reason="work",
        subtask="edit",
        acceptance_criteria=("pass",),
    )
    report = AuditReport(status="complete", summary="ok", evidence=("test",), verified_facts=("done",))
    record = RoundRecord(
        round_index=1,
        decision=decision,
        executor=episode,
        auditor=episode,
        audit=report,
    )
    assert parse_round_record(json.dumps(record.to_dict())) == record
    with pytest.raises(SchemaError):
        parse_round_record(json.dumps({**record.to_dict(), "round_index": 0}))
