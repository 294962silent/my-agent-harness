"""CPU-only Chinese memory profile: explicit artifacts, no remote credentials."""
import hashlib
import json

import pytest


def test_local_profile_is_explicit_and_has_no_credentials(tmp_path):
    from my_agent_core.memory_local import local_cpu_config
    config = local_cpu_config(model_dir=tmp_path / "model", storage_dir=tmp_path / "store")
    assert config["profile"] == "local_cpu_zh"
    assert config["threads"] == 2
    assert not any(word in json.dumps(config).lower() for word in ("api_key", "secret", "sentinel"))


def test_missing_or_changed_model_rejected_before_sdk_load(tmp_path, monkeypatch):
    from my_agent_core import memory_local
    with pytest.raises(ValueError, match="model"):
        memory_local.verify_model_files(tmp_path)
    files = {name: ("fake-" + name).encode() for name in memory_local.MODEL_FILES}
    monkeypatch.setattr(memory_local, "MODEL_SHA256", hashlib.sha256(files["model_optimized.onnx"]).hexdigest())
    monkeypatch.setattr(memory_local, "MODEL_BLOBS", {
        name: hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
        for name, body in files.items() if name != "model_optimized.onnx"
    })
    for name, body in files.items():
        (tmp_path / name).write_bytes(body)
    manifest = {
        "repository": memory_local.MODEL_REPO, "revision": memory_local.MODEL_REVISION,
        "files": [{"file": name, "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body)}
                  for name, body in files.items()],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert memory_local.verify_model_files(tmp_path)["revision"] == memory_local.MODEL_REVISION
    (tmp_path / "tokenizer.json").write_text("changed")
    with pytest.raises(ValueError, match="model"):
        memory_local.verify_model_files(tmp_path)
    # Updating the local manifest cannot bless different upstream bytes.
    manifest["files"] = [memory_local._fingerprint(tmp_path / name) for name in memory_local.MODEL_FILES]
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="model"):
        memory_local.verify_model_files(tmp_path)


def test_local_profile_dispatch_preserves_single_worker_adapter(monkeypatch, tmp_path):
    from my_agent_core import memory_local
    from my_agent_core.memory_mem0 import create_mem0_index
    seen = {}
    expected = object()

    def factory(config, *, timeout):
        seen.update(config=config, timeout=timeout)
        return expected

    monkeypatch.setattr(memory_local, "create_local_cpu_index", factory)
    config = memory_local.local_cpu_config(model_dir=tmp_path / "model", storage_dir=tmp_path / "store")
    assert create_mem0_index(config, timeout=4) is expected
    assert seen == {"config": config, "timeout": 4}


def test_preparation_uses_only_fixed_public_revision_and_never_overwrites(tmp_path, monkeypatch):
    import io
    from my_agent_core import memory_local
    files = {name: ("test-" + name).encode() for name in memory_local.MODEL_FILES}
    monkeypatch.setattr(memory_local, "MODEL_SHA256", hashlib.sha256(files["model_optimized.onnx"]).hexdigest())
    monkeypatch.setattr(memory_local, "MODEL_BLOBS", {
        name: hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest()
        for name, body in files.items() if name != "model_optimized.onnx"
    })
    urls = []

    def download(request, **kwargs):
        url = request.full_url
        urls.append(url)
        assert url.startswith(f"https://huggingface.co/{memory_local.MODEL_REPO}/resolve/{memory_local.MODEL_REVISION}/")
        assert "Authorization" not in request.headers
        return io.BytesIO(files[url.rsplit("/", 1)[1]])

    monkeypatch.setattr(memory_local, "urlopen", download)
    monkeypatch.setattr(memory_local, "getproxies", lambda: {})
    destination = tmp_path / "model"
    first = memory_local.prepare_model_cache(destination)
    assert first == memory_local.prepare_model_cache(destination)
    assert len(urls) == len(memory_local.MODEL_FILES)
    (destination / "model_optimized.onnx").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        memory_local.prepare_model_cache(destination)
    assert len(urls) == len(memory_local.MODEL_FILES)


def test_invalid_timeout_rejected_before_model_loading(tmp_path):
    from my_agent_core.memory_local import create_local_cpu_index, local_cpu_config
    config = local_cpu_config(model_dir=tmp_path / "absent", storage_dir=tmp_path / "cache")
    with pytest.raises(ValueError, match="timeout"):
        create_local_cpu_index(config, timeout=-1)


@pytest.mark.parametrize("proxy", ["http://user:password@proxy.invalid", "http://:password@proxy.invalid", "http://:@proxy.invalid"])
def test_public_download_rejects_proxy_credentials_before_any_transport(tmp_path, monkeypatch, proxy):
    from my_agent_core import memory_local
    from urllib.request import Request
    monkeypatch.setattr(memory_local, "getproxies", lambda: {"https": proxy})
    monkeypatch.setattr(memory_local, "urlopen", lambda *a, **kw: pytest.fail("attempted authenticated transport"))
    with pytest.raises(ValueError, match="authenticated proxy"):
        memory_local._download_public(Request("https://huggingface.co/public"), tmp_path / "public")


def test_partial_initialization_cleanup_attempts_all_resources_without_masking_original_error():
    from types import SimpleNamespace
    from my_agent_core import memory_local
    attempts = []

    def failing_close(name):
        def close():
            attempts.append(name)
            raise RuntimeError("cleanup failure")
        return close

    memory = SimpleNamespace(embedding_model=SimpleNamespace(close=failing_close("embedding")),
                             vector_store=SimpleNamespace(client=SimpleNamespace(close=failing_close("vector"))))
    with pytest.raises(ValueError, match="initialization failure"):
        try:
            raise ValueError("initialization failure")
        except ValueError:
            memory_local._rollback_local_memory(memory, failing_close("cache"))
            raise
    assert attempts == ["embedding", "vector", "cache"]
