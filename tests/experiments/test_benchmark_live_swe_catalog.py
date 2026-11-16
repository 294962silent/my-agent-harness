"""Frozen, host-approved catalogs generalize tasks without weakening solver gates."""

import json

import pytest

from experiments.benchmark_live import swe_agent as swe
from my_agent_llm import LLM, Response


def catalog_fixture(tmp_path):
    """Synthetic third task; never loads an evaluator patch or test body."""
    instance = "django__django-11099"
    task = {
        "instance_id": instance,
        "repo": "django/django",
        "base_commit": "a" * 40,
        "image": "swebench/sweb.eval.x86_64.django_1776_django-11099:latest",
        "problem_statement": "SYNTHETIC_SAFE_PROBLEM",
    }
    metadata = tmp_path / "metadata.json"
    metadata.write_text(json.dumps(task))
    pin = {k: task[k] for k in ("instance_id", "repo", "base_commit", "image")}
    pin.update(image_id="sha256:" + "b" * 64, metadata_sha256=swe.sha(metadata))
    data = {
        "schema_version": 1,
        "source": {"repository": "https://github.com/SWE-bench/swe-bench-tasks", "revision": "c" * 40},
        "tasks": [pin],
    }
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps(data))
    report = tmp_path / "official.json"
    report.write_text(
        json.dumps(
            {"resolved_ids": [instance], "completed_instances": 1, "resolved_instances": 1, "error_instances": 0}
        )
    )
    evidence = tmp_path / "evidence.json"
    evidence.write_text(
        json.dumps(
            {
                **{k: pin[k] for k in ("instance_id", "base_commit", "image", "image_id", "metadata_sha256")},
                "task_repository_revision": "c" * 40,
                "resolved": True,
                "official_report": str(report),
                "official_report_sha256": swe.sha(report),
            }
        )
    )
    return metadata, evidence, catalog, data


def load(path):
    from experiments.benchmark_live.swe_catalog import load_catalog

    return load_catalog(path, expected_sha256=swe.sha(path))


def test_third_task_catalog_is_frozen_and_gates_exact_safe_metadata(tmp_path):
    metadata, evidence, path, data = catalog_fixture(tmp_path)
    catalog = load(path)
    spec = catalog.get(data["tasks"][0]["instance_id"])
    assert spec.repo == "django/django" and spec.source_revision == "c" * 40
    assert catalog.sha256 == swe.sha(path)
    task, _ = swe.gated_task(metadata, evidence, catalog=catalog)
    assert task["instance_id"] == spec.instance_id
    with pytest.raises(ValueError, match="catalog"):
        catalog.get("missing__repo-1")
    docker = swe.DockerTask(spec)
    assert docker.pin["base_commit"] == spec.base_commit and docker.pin["image_id"] == spec.image_id


def test_catalog_hash_detects_changes_before_parsing(tmp_path):
    from experiments.benchmark_live.swe_catalog import load_catalog

    _, _, path, _ = catalog_fixture(tmp_path)
    expected = swe.sha(path)
    path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError, match="checksum"):
        load_catalog(path, expected_sha256=expected)


@pytest.mark.parametrize(
    "mutation",
    [
        "root_extra",
        "source_extra",
        "gold_field",
        "revision",
        "source",
        "duplicate",
        "repo",
        "base",
        "image",
        "image_id",
        "metadata_sha",
        "instance",
    ],
)
def test_catalog_rejects_unknown_fields_and_unbound_or_unsafe_pins(tmp_path, mutation):
    _, _, path, data = catalog_fixture(tmp_path)
    row = data["tasks"][0]
    if mutation == "root_extra":
        data["patch"] = "FORBIDDEN"
    elif mutation == "source_extra":
        data["source"]["test_patch"] = "FORBIDDEN"
    elif mutation == "gold_field":
        row["gold.patch"] = "FORBIDDEN"
    elif mutation == "revision":
        data["source"]["revision"] = "main"
    elif mutation == "source":
        data["source"]["repository"] = "https://example.com/tasks"
    elif mutation == "duplicate":
        data["tasks"].append(dict(row))
    elif mutation == "repo":
        row["repo"] = "other/repo"
    elif mutation == "base":
        row["base_commit"] = "a; touch /tmp/unwanted"
    elif mutation == "image":
        row["image"] = "attacker/other:latest"
    elif mutation == "image_id":
        row["image_id"] = "latest"
    elif mutation == "metadata_sha":
        row["metadata_sha256"] = "unknown"
    elif mutation == "instance":
        row["instance_id"] = "../host"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        load(path)


@pytest.mark.parametrize("mutation", ["metadata", "base", "image", "image_id", "revision", "report", "gold_field"])
def test_catalog_gate_rejects_changed_bindings(tmp_path, mutation):
    metadata, evidence, path, _ = catalog_fixture(tmp_path)
    catalog = load(path)
    task, gate = json.loads(metadata.read_text()), json.loads(evidence.read_text())
    if mutation == "metadata":
        task["problem_statement"] += " changed"
    elif mutation == "base":
        task["base_commit"] = "d" * 40
    elif mutation == "image":
        task["image"] = "swebench/other:latest"
    elif mutation == "image_id":
        gate["image_id"] = "sha256:" + "d" * 64
    elif mutation == "revision":
        gate["task_repository_revision"] = "d" * 40
    elif mutation == "gold_field":
        task["test_patch"] = "FORBIDDEN"
    elif mutation == "report":
        report = tmp_path / "official.json"
        report.write_text("{}")
        gate["official_report_sha256"] = swe.sha(report)
    metadata.write_text(json.dumps(task))
    evidence.write_text(json.dumps(gate))
    with pytest.raises(ValueError):
        swe.gated_task(metadata, evidence, catalog=catalog)


@pytest.mark.anyio
async def test_third_catalog_task_runs_same_guarded_agent_and_persists_source(tmp_path, monkeypatch):
    metadata, evidence, path, _ = catalog_fixture(tmp_path)
    catalog = load(path)
    monkeypatch.setenv("LIVE_BENCH_API_KEY", "SYNTHETIC_HOST_KEY")
    seen = []
    ledger_options = []
    original_ledger = swe.BudgetLedger

    def ledger_factory(path, **kwargs):
        ledger_options.append(kwargs)
        return original_ledger(path)

    monkeypatch.setattr(swe, "BudgetLedger", ledger_factory)

    class Provider:
        async def achat(self, messages, **kwargs):
            seen.extend(m.text_content or "" for m in messages)
            return Response(content="done", model="fake", usage={"prompt_tokens": 30, "completion_tokens": 5})

    def factory(config):
        assert config.max_retries == 0
        llm = LLM(config)
        llm._provider = Provider()
        return llm

    class Environment:
        async def start(self):
            pass

        async def patch(self):
            return "SYNTHETIC_MODEL_DIFF"

        async def close(self):
            pass

    root = await swe.solve(
        metadata=metadata,
        gold_evidence=evidence,
        catalog=catalog,
        ledger_path=tmp_path / "ledger.json",
        arm="on",
        output=tmp_path / "run",
        environment=Environment(),
        llm_factory=factory,
        run_id="frozen-trial-id",
        spend_ceiling_usd=1.0,
    )
    result = json.loads((root / "run.json").read_text())
    assert result["instance_id"] == "django__django-11099"
    assert result["catalog_sha256"] == catalog.sha256 and result["task_repository_revision"] == "c" * 40
    assert result["run_id"] == "frozen-trial-id" and result["requests"] == 1
    assert ledger_options == [{"spend_ceiling_usd": 1.0}]
    assert "SYNTHETIC_SAFE_PROBLEM" in "\n".join(seen)
    assert not any(secret in "\n".join(seen) for secret in ("SYNTHETIC_HOST_KEY", "official_report", "resolved_ids"))


@pytest.mark.parametrize("flag,value", [("--catalog", "catalog.json"), ("--catalog-sha256", "a" * 64)])
def test_cli_requires_catalog_and_hash_together(flag, value, capsys):
    with pytest.raises(SystemExit):
        swe.main(
            [
                "--metadata",
                "unused",
                "--gold-evidence",
                "unused",
                "--ledger-path",
                "unused",
                "--arm",
                "on",
                "--output",
                "unused",
                flag,
                value,
            ]
        )
    assert "must be supplied together" in capsys.readouterr().err


def test_cli_loads_frozen_catalog_and_forwards_campaign_limits(tmp_path, monkeypatch):
    metadata, evidence, path, _ = catalog_fixture(tmp_path)
    received = []

    async def fake_solve(**kwargs):
        received.append(kwargs)

    monkeypatch.setattr(swe, "solve", fake_solve)
    swe.main(
        [
            "--metadata",
            str(metadata),
            "--gold-evidence",
            str(evidence),
            "--ledger-path",
            "ledger",
            "--arm",
            "off",
            "--output",
            "unused",
            "--catalog",
            str(path),
            "--catalog-sha256",
            swe.sha(path),
            "--spend-ceiling-usd",
            "3.068597",
            "--run-id",
            "campaign-before-start",
        ]
    )
    assert received[0]["catalog"].sha256 == swe.sha(path)
    assert received[0]["spend_ceiling_usd"] == 3.068597 and received[0]["run_id"] == "campaign-before-start"
