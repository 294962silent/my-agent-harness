# pyright: reportArgumentType=false
"""Artifact store: stable refs, bounded previews, paging, ownership, no recursion."""

from __future__ import annotations

import pytest

from my_agent_core.artifacts import (
    PREVIEW_THRESHOLD,
    ArtifactError,
    ArtifactStore,
    apply_previews,
    looks_truncated,
    render_preview,
)
from my_agent_llm import Message


def test_same_tool_call_id_different_content_never_overwrites(tmp_path):
    store = ArtifactStore(tmp_path)
    a = store.save("AAAA\n" * 10, tool_name="bash", tool_call_id="call_1")
    b = store.save("BBBB\n" * 10, tool_name="bash", tool_call_id="call_1")
    assert a.artifact_id != b.artifact_id
    assert store.read_page(a.handle(), cursor=0)["text"].startswith("AAAA")
    assert store.read_page(b.handle(), cursor=0)["text"].startswith("BBBB")


def test_same_content_same_ref_and_repeat_preview_is_stable(tmp_path):
    store = ArtifactStore(tmp_path)
    text = "x" * (PREVIEW_THRESHOLD + 100) + "ERROR at tail"
    p1, m1 = render_preview(text, store, tool_name="bash", tool_call_id="c1")
    p2, m2 = render_preview(text, store, tool_name="bash", tool_call_id="c2")
    assert m1["artifact_id"] == m2["artifact_id"]
    assert p1 == p2
    assert "ERROR at tail" in p1  # tail (including errors) is visible
    assert p1.startswith("[ARTIFACT ")


def test_unicode_paging_reconstructs_exactly(tmp_path):
    store = ArtifactStore(tmp_path)
    text = ("汉字🙂abc" * 5000) + "\nEND"
    ref = store.save(text, tool_name="bash", tool_call_id="u")
    pages, cursor = [], 0
    while cursor is not None:
        page = store.read_page(ref.handle(), cursor=cursor, max_bytes=1000)
        pages.append(page["text"])
        cursor = page["next_cursor"]
    assert "".join(pages) == text


def test_long_single_line_is_not_skipped(tmp_path):
    store = ArtifactStore(tmp_path)
    text = "A" * 5000 + "MARKER" + "B" * 5000
    ref = store.save(text, tool_name="bash", tool_call_id="long")
    page = store.read_page(ref.handle(), cursor=0, max_bytes=1024)
    assert page["truncated"] is True
    p2 = store.read_page(ref.handle(), cursor=page["next_cursor"], max_bytes=8192)
    assert "MARKER" in p2["text"]
    found = store.search(ref.handle(), "MARKER")
    assert found["hits"] and "MARKER" in found["hits"][0]["text"]


def test_foreign_owner_and_path_traversal_rejected(tmp_path):
    other = ArtifactStore(tmp_path / "other")
    ref = other.save("secret", tool_name="bash", tool_call_id="x")
    mine = ArtifactStore(tmp_path)
    with pytest.raises(ArtifactError):
        mine.resolve(ref.handle())
    with pytest.raises(ArtifactError):
        mine.resolve("../../etc/passwd")
    with pytest.raises(ArtifactError):
        mine.resolve("artifact:deadbeefdeadbeef:../../x")


def test_truncated_source_gets_no_fake_handle(tmp_path):
    store = ArtifactStore(tmp_path)
    text = "[Output truncated: showing last 5 lines]\nbody" + "x" * PREVIEW_THRESHOLD
    preview, meta = render_preview(text, store, tool_name="bash", tool_call_id="t")
    assert meta["full_unavailable"] == "source_truncated"
    assert meta["artifact_id"] is None
    assert "ARTIFACT UNAVAILABLE" in preview
    assert "artifact:" not in preview


def test_previews_do_not_mutate_original_and_skip_trusted_tools(tmp_path):
    store = ArtifactStore(tmp_path)
    big = "y" * (PREVIEW_THRESHOLD + 50)
    original = Message(role="tool", content=big, metadata={"tool_name": "bash", "tool_call_id": "c"})
    trusted = Message(
        role="tool",
        content=big,
        metadata={"tool_name": "artifact_read", "tool_call_id": "d", "artifact_bounded": True},
    )
    view, changed = apply_previews([original, trusted], store)
    assert changed is True
    assert original.content == big  # Session-style object untouched
    assert view[0].content.startswith("[ARTIFACT artifact:")
    assert view[1] is trusted  # bounded trusted tool not wrapped again


def test_marker_detection():
    assert looks_truncated("... [Output truncated: showing last 3 lines]")
    assert looks_truncated("ordinary output") is None


def test_invalid_unicode_cursor_fails_without_stalling(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.save("🙂" * 1000, tool_name="bash", tool_call_id="u")
    with pytest.raises(ArtifactError, match="boundary"):
        store.read_page(ref.handle(), cursor=1)
    with pytest.raises(ArtifactError, match="outside"):
        store.read_page(ref.handle(), cursor=-1)


def test_projected_view_is_idempotent_and_never_saved_as_source(tmp_path):
    store = ArtifactStore(tmp_path)
    original = Message(role="tool", content="Q" * 10000, metadata={"tool_name": "bash"})
    view, _ = apply_previews([original], store)
    again, changed = apply_previews(view, store)
    assert changed is False
    assert again[0].content == view[0].content
    assert len(list(store._owner_dir.glob("*.txt"))) == 1


def test_save_failure_advertises_no_handle(tmp_path, monkeypatch):
    from pathlib import Path

    def denied(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "mkdir", denied)
    preview, meta = render_preview("z" * 10000, ArtifactStore(tmp_path), tool_name="bash", tool_call_id="x")
    assert meta["full_unavailable"] == "save_failed"
    assert meta["artifact_id"] is None
    assert "artifact:" not in preview


def test_missing_and_corrupt_artifact_are_explicit(tmp_path):
    store = ArtifactStore(tmp_path)
    ref = store.save("old", tool_name="bash", tool_call_id="x")
    ref.path.write_text("tampered", encoding="utf-8")
    with pytest.raises(ArtifactError, match="hash mismatch"):
        store.read_page(ref.handle())
    ref.path.unlink()
    with pytest.raises(ArtifactError, match="cleaned up"):
        store.read_page(ref.handle())


def test_resume_workspace_owner_retains_old_handles(tmp_path):
    first = ArtifactStore(tmp_path)
    ref = first.save("existing history", tool_name="bash", tool_call_id="x")
    resumed = ArtifactStore(tmp_path)
    assert resumed.owner == first.owner
    assert resumed.read_page(ref.handle())["text"] == "existing history"


def test_external_link_storage_rejected(tmp_path):
    outside = tmp_path.parent / (tmp_path.name + "-outside")
    outside.mkdir()
    redirected = tmp_path / ".my_agent_core"
    try:
        redirected.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlink permission unavailable")
    with pytest.raises(ArtifactError):
        ArtifactStore(tmp_path).save("secret", tool_name="bash", tool_call_id="x")
    assert not list(outside.iterdir())


def test_search_caps_and_reports_actual_extra_hits(tmp_path):
    store = ArtifactStore(tmp_path)
    one = store.save("MARKER\nother", tool_name="bash", tool_call_id="s")
    assert store.search(one.handle(), "MARKER", max_results=1)["truncated"] is False
    many = store.save("MARKER\n" * 30, tool_name="bash", tool_call_id="s")
    found = store.search(many.handle(), "MARKER", max_results=10000)
    assert len(found["hits"]) == 20
    assert found["truncated"] is True
    with pytest.raises(ArtifactError, match="200"):
        store.search(one.handle(), "x" * 201)
