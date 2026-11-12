"""Memory privacy, complete reads, explicit deletion and persistence failure contracts."""

import json

import pytest

from my_agent_core.memory import MemoryStore


def store_at(tmp_path):
    store = MemoryStore(tmp_path)
    store.load_from_disk()
    return store


def test_memory_redaction_before_persistence_and_outputs(tmp_path):
    from my_agent_core.memory_safety import redact_memory_text

    secret = "sk-" + "a" * 32
    clean = redact_memory_text(f"port=8080 中文偏好简洁 api_key={secret} Bearer xyz.abc.123 password=hunter2")
    assert secret not in clean and "hunter2" not in clean and "xyz.abc.123" not in clean
    assert "port=8080 中文偏好简洁" in clean
    store = store_at(tmp_path)
    store.add("memory", f"Project api_key={secret}")
    result = store.remember("api_key", secret, source=f"Bearer {secret}")
    assert secret not in result
    assert secret not in "".join(p.read_text(encoding="utf-8-sig") for p in tmp_path.rglob("*.md"))
    assert secret not in store.read_full("api_key")


def test_read_delete_exact_identity_and_reload_no_resurrection(tmp_path):
    store = store_at(tmp_path)
    store.remember("城市", "上海")
    store.remember("颜色", "蓝色")
    store.remember("城市", "北京")
    full = json.loads(store.read_full("城市"))
    assert full["value"] == "北京" and full["history"][0]["value"] == "上海"
    assert "Deleted" in store.delete("城市")
    assert "not found" in store.read_full("城市").lower()
    assert json.loads(store.read_full("颜色"))["value"] == "蓝色"
    reloaded = store_at(tmp_path)
    assert "not found" in reloaded.read_full("城市").lower()
    assert "北京" not in (reloaded.retrieve("城市") or "")
    assert not list(tmp_path.glob("facts/**/untitled.md"))


def test_long_fact_preview_exposes_exact_lookup(tmp_path):
    store = store_at(tmp_path)
    store.remember("deployment_notes", "deployment " * 1000)
    result = store.retrieve("deployment", budget_chars=220)
    assert result and "deployment_notes" in result and "user" in result
    assert "memory_read" in result and "truncated" in result
    assert len(result) <= 280
    assert len(json.loads(store.read_full("deployment_notes"))["value"]) > 1000


def test_free_entries_search_immediately_and_rollback_on_failure(tmp_path, monkeypatch):
    store = store_at(tmp_path)
    store.add("memory", "zephyr project uses pytest")
    assert "zephyr" in store.retrieve("zephyr")
    snapshot = store.format_all_for_system_prompt()

    def fail(target):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_atomic_save", fail)
    with pytest.raises(OSError):
        store.replace("memory", "zephyr", "lost entry")
    assert "zephyr" in store.retrieve("zephyr")
    assert store.format_all_for_system_prompt() == snapshot


def test_failed_fact_save_does_not_claim_or_cache_success(tmp_path, monkeypatch):
    import my_agent_core.memory as memory_module

    store = store_at(tmp_path)
    store.remember("port", "8080")

    def fail(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(memory_module, "save_fact", fail)
    with pytest.raises(OSError):
        store.remember("port", "9000")
    assert json.loads(store.read_full("port"))["value"] == "8080"


@pytest.mark.anyio
async def test_memory_read_and_delete_tools(tmp_path):
    from my_agent_core.memory import make_memory_read_tool, make_memory_tool

    store = store_at(tmp_path)
    store.remember("port", "8080")
    result = await make_memory_read_tool(store).execute({"key": "port"})
    assert "8080" in str(result.data)
    result = await make_memory_tool(store).execute({"target": "user", "action": "delete", "key": "port"})
    assert result.ok and "Deleted" in str(result.data)


def test_same_fact_is_noop_preserves_history(tmp_path):
    store = store_at(tmp_path)
    store.remember("port", "8080")
    store.remember("port", "9000")
    before = store.read_full("port")
    count = len(store._facts)
    assert "unchanged" in store.remember("port", "9000")
    assert len(store._facts) == count
    assert store.read_full("port") == before


def test_delete_prevents_legacy_revival_and_allows_explicit_recreation(tmp_path):
    store = store_at(tmp_path)
    store.remember("port", "8080")
    store.delete("port")
    (tmp_path / "FACTS.jsonl").write_text(
        json.dumps({"subject": "user", "key": "port", "value": "8080"}), encoding="utf-8"
    )
    again = store_at(tmp_path)
    assert "not found" in again.read_full("port").lower()
    again.remember("port", "9000")
    assert json.loads(store_at(tmp_path).read_full("port"))["value"] == "9000"


def test_legacy_untitled_exact_identity_is_readable_and_deletable(tmp_path):
    from my_agent_core.memory_index import Fact, fact_to_markdown

    subject, key = "\u9879\u76ee", "\u57ce\u5e02"
    other_subject, other_key = "\u7528\u6237", "\u989c\u8272"
    old = tmp_path / "facts" / "untitled" / "untitled.md"
    old.parent.mkdir(parents=True)
    old.write_text(fact_to_markdown(Fact(subject, key, "legacy")), encoding="utf-8")
    store = store_at(tmp_path)
    assert json.loads(store.read_full(key, subject))["value"] == "legacy"
    store.remember(other_key, "other", subject=other_subject)
    store.delete(key, subject)
    assert not old.exists()
    assert json.loads(store_at(tmp_path).read_full(other_key, other_subject))["value"] == "other"


def test_read_rejects_paths_and_preserves_multiline_metadata(tmp_path):
    store = store_at(tmp_path)
    for name in ("../port", "/port", "..", "x\\port"):
        with pytest.raises(ValueError, match="not paths"):
            store.read_full(name)
    text = 'first\n---\nlast\\path "quoted"'
    store.remember("notes", text, source="local document")
    store.remember("notes", text + "\nnew", source="second document")
    full = json.loads(store_at(tmp_path).read_full("notes"))
    assert full["value"] == text + "\nnew"
    assert full["history"][0]["value"] == text
    assert full["history"][0]["source"] == "local document"


def test_loaded_historical_secret_is_redacted_without_rewriting(tmp_path):
    from my_agent_core.memory_index import Fact, fact_to_markdown

    raw_secret = "sk-" + "z" * 32
    path = tmp_path / "facts" / "user" / "notes.md"
    path.parent.mkdir(parents=True)
    raw = fact_to_markdown(Fact("user", "notes", f"old api_key={raw_secret}", source=f"Bearer {raw_secret}"))
    path.write_text(raw, encoding="utf-8")
    store = store_at(tmp_path)
    assert raw_secret not in store.read_full("notes")
    assert raw_secret not in (store.retrieve("notes") or "")
    assert path.read_text(encoding="utf-8") == raw


def test_updated_legacy_identity_wins_on_reload(tmp_path):
    from my_agent_core.memory_index import Fact, fact_to_markdown

    key = "\u57ce\u5e02"
    old = tmp_path / "facts" / "user" / "untitled.md"
    old.parent.mkdir(parents=True)
    old.write_text(fact_to_markdown(Fact("user", key, "old", valid_from="2020-01-01")), encoding="utf-8")
    store = store_at(tmp_path)
    store.remember(key, "new", valid_from="2021-01-01")
    loaded = store_at(tmp_path)
    full = json.loads(loaded.read_full(key))
    assert full["value"] == "new"
    assert full["history"][0]["value"] == "old"
    loaded.delete(key)
    assert not list(tmp_path.glob("facts/**/*.md"))


def test_failed_delete_restores_disk_and_index(tmp_path, monkeypatch):
    import my_agent_core.memory as memory_module

    store = store_at(tmp_path)
    store.remember("port", "8080")
    from my_agent_core.memory_index import atomic_write_text

    def marker_failure(path, text):
        if path.name == ".deleted.json":
            raise OSError("disk full")
        return atomic_write_text(path, text)

    monkeypatch.setattr(memory_module, "atomic_write_text", marker_failure)
    with pytest.raises(OSError):
        store.delete("port")
    assert json.loads(store.read_full("port"))["value"] == "8080"
    assert json.loads(store_at(tmp_path).read_full("port"))["value"] == "8080"


def test_redaction_formats_and_idempotence():
    from my_agent_core.memory_safety import redact_memory_text

    fixtures = [
        "token=short-token",
        'PASSWORD: "a secret with spaces"',
        "Authorization: Bearer eyJhbGc.eyJ1c2Vy.signature",
        "ghp_" + "a" * 30,
        "sk-ant-api03-" + "a" * 30,
        "-----BEGIN RSA PRIVATE KEY-----\nencoded-secret\n-----END RSA PRIVATE KEY-----",
    ]
    for original in fixtures:
        clean = redact_memory_text(original)
        assert "[REDACTED]" in clean and clean != original
        assert redact_memory_text(clean) == clean
    assert redact_memory_text("port=8080 timeout=30") == "port=8080 timeout=30"


def test_redaction_preserves_ordinary_token_configuration(tmp_path):
    from my_agent_core.memory_safety import redact_memory_text, redact_memory_value

    plain = "max_tokens=4096 context_token_budget=16000 token_budget=3200 secret_scanning_enabled=false port=8080"
    assert redact_memory_text(plain) == plain
    for key in ("max_tokens", "context_token_budget", "token_budget", "secret_scanning_enabled"):
        assert redact_memory_value(key, "4096") == "4096"
    for key in ("api_key", "access_token", "refresh_token", "wifi_password", "token", "OPENAI_API_KEY"):
        assert redact_memory_value(key, "short") == "[REDACTED]"
        assert "short" not in redact_memory_text(f"{key}=short")
    store = store_at(tmp_path)
    store.remember("context_token_budget", "16000")
    assert json.loads(store.read_full("context_token_budget"))["value"] == "16000"


def test_case_sensitive_value_change_supersedes(tmp_path):
    store = store_at(tmp_path)
    store.remember("project_name", "Alpha")
    store.remember("project_name", "alpha")
    active = [fact for fact in store._facts if fact.active]
    assert len(active) == 1 and active[0].value == "alpha"
    assert json.loads(store_at(tmp_path).read_full("project_name"))["history"][0]["value"] == "Alpha"


def test_chinese_subjects_with_same_key_do_not_share_files(tmp_path):
    subjects = ("\u9879\u76ee", "\u7528\u6237")
    store = store_at(tmp_path)
    store.remember("port", "8080", subject=subjects[0])
    store.remember("port", "9000", subject=subjects[1])
    reloaded = store_at(tmp_path)
    assert json.loads(reloaded.read_full("port", subjects[0]))["value"] == "8080"
    assert json.loads(reloaded.read_full("port", subjects[1]))["value"] == "9000"
    reloaded.delete("port", subjects[0])
    assert json.loads(store_at(tmp_path).read_full("port", subjects[1]))["value"] == "9000"
