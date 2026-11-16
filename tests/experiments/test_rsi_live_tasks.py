"""Task material separation and real local evaluator preflight; no provider calls."""
import json
import sys
import time

import pytest

from experiments.context_rsi import live_tasks as tasks


def rows():
    return [{"task_id": task_id, "entry_point": "solve",
             "prompt": f'def solve(x):\n    """Task {task_id}.\n    >>> solve(2)\n    3\n    """\n',
             "canonical_solution": "    return x + 1\n", "test": "def check(candidate):\n    assert candidate(7) == 8\n"}
            for task_id in tasks.FROZEN_IDS]


def prepare(tmp_path, data=None):
    return tasks._prepare_rows(tmp_path / "prepared", tmp_path / "private", sys.executable,
                               rows() if data is None else data, {"fixture": True})


def test_preflight_and_prompt_only_source(tmp_path):
    manifest = prepare(tmp_path)
    prepared = tasks.load_tasks(manifest)
    assert len(prepared) == 11
    assert {t.split for t in prepared} == {"train", "val", "test"}
    for task in prepared:
        task.verify()
        assert [p.name for p in task.source_dir.iterdir()] == ["solution.py"]
        assert "return x + 1" not in task.goal
        assert "candidate(7)" not in task.goal
        assert task.preflight_proof["visible_empty"] == 0
        assert task.preflight_proof["visible_gold"] == 1
        assert task.preflight_proof["final_empty"] == 0
        assert task.preflight_proof["final_gold"] == 1
    with pytest.raises(FileExistsError):
        prepare(tmp_path)


def test_no_visible_cases_blocks_entire_batch(tmp_path):
    data = rows()
    data[0]["prompt"] = 'def solve(x):\n    """No examples."""\n'
    with pytest.raises(tasks.TaskPreparationError, match="visible examples"):
        prepare(tmp_path, data)
    assert not (tmp_path / "prepared").exists()


def test_visible_examples_selected_by_entry_point_not_first_helper():
    row = {"task_id": "HumanEval/32", "entry_point": "find_zero",
           "prompt": 'def poly(x):\n    """No examples for helper."""\n    return x\n\ndef find_zero(xs):\n    """Find a root.\n    >>> find_zero([1, 2])\n    -0.5\n    """\n'}
    assert tasks._visible_count(row) == 1


@pytest.mark.parametrize("mutation", ["overlap", "duplicate", "gold_fails"])
def test_invalid_batch_is_rejected(tmp_path, mutation):
    data = rows()
    if mutation == "overlap":
        data[1]["task_id"] = data[0]["task_id"]
    elif mutation == "duplicate":
        data[1]["prompt"] = data[0]["prompt"]
    else:
        data[0]["canonical_solution"] = "    return 0\n"
    with pytest.raises(tasks.TaskPreparationError):
        prepare(tmp_path, data)
    assert not (tmp_path / "prepared" / "manifest.json").exists()


@pytest.mark.parametrize("target", ["source", "evaluator", "private_task", "final_task", "worker", "manifest"])
def test_tampering_rejected(tmp_path, target):
    manifest = prepare(tmp_path)
    task = tasks.load_tasks(manifest)[0]
    path = {"source": task.source_dir / "solution.py", "evaluator": task._evaluator,
            "private_task": task._host_task, "final_task": task._final_task,
            "worker": task._worker, "manifest": manifest}[target]
    path.write_text(path.read_text() + "\n", encoding="utf-8")
    with pytest.raises(tasks.TaskPreparationError, match="hash"):
        task.verify()
    with pytest.raises(tasks.TaskPreparationError):
        tasks.load_tasks(manifest)


def test_evaluator_ignores_branch_tests_and_strips_credentials(tmp_path, monkeypatch):
    manifest = prepare(tmp_path)
    task = tasks.load_tasks(manifest)[0]
    (task.source_dir / "test.py").write_text("raise SystemExit(0)")
    assert tasks._evaluate(task.visible_argv, task.source_dir) == 0
    (task.source_dir / "solution.py").write_text(
        'import os\nassert "DEEPSEEK_API_KEY" not in os.environ\ndef solve(x): return x + 1\n')
    monkeypatch.setenv("DEEPSEEK_API_KEY", "fixture-do-not-inherit")
    assert tasks._evaluate(task.visible_argv, task.source_dir) == 1


def test_timeout_fails_closed(tmp_path):
    script = tmp_path / "hang.py"
    script.write_text("import time\ntime.sleep(20)")
    with pytest.raises(tasks.TaskPreparationError, match="timeout"):
        tasks._evaluate((sys.executable, "-I", str(script)), tmp_path, timeout=0.1)


def test_timeout_cleans_owned_descendant(tmp_path):
    marker = tmp_path / "orphan-marker"
    child_code = "import time,pathlib;time.sleep(2);pathlib.Path(" + repr(str(marker)) + ").write_text('orphan')"
    script = tmp_path / "spawn.py"
    script.write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-I','-c'," + repr(child_code) + "])\ntime.sleep(20)\n")
    with pytest.raises(tasks.TaskPreparationError, match="timeout"):
        tasks._evaluate((sys.executable, "-I", str(script)), tmp_path, timeout=0.7)
    time.sleep(2.1)
    assert not marker.exists()


def test_recomputed_manifest_hash_does_not_allow_identity_drift(tmp_path):
    manifest = prepare(tmp_path)
    data = json.loads(manifest.read_text())
    data["tasks"][0]["split"] = "test"
    tasks._json(manifest, data)
    manifest.with_name("manifest.sha256").write_text(tasks._sha(manifest))
    with pytest.raises(tasks.TaskPreparationError, match="identity drift"):
        tasks.load_tasks(manifest)


def test_candidate_cannot_monkeypatch_host_doctest_checker(tmp_path):
    task = tasks.load_tasks(prepare(tmp_path))[0]
    original = (task.source_dir / "solution.py").read_text()
    (task.source_dir / "solution.py").write_text(original +
        "\nimport doctest\ndoctest.DocTestRunner.run = lambda *args, **kwargs: doctest.TestResults(0, 1)\n")
    assert tasks._evaluate(task.visible_argv, task.source_dir) == 0


def test_visible_candidate_cannot_read_final_tests_from_main(tmp_path):
    task = tasks.load_tasks(prepare(tmp_path))[0]
    (task.source_dir / "solution.py").write_text(
        "import __main__\nassert 'test' not in getattr(__main__, 't', {})\ndef solve(x): return x + 1\n")
    assert tasks._evaluate(task.visible_argv, task.source_dir) == 1


def test_payloads_are_physically_separate_and_visible_never_reads_final(tmp_path):
    task = tasks.load_tasks(prepare(tmp_path))[0]
    visible = json.loads(task._host_task.read_text())
    final = json.loads(task._final_task.read_text())
    assert set(visible) == {"task_id", "prompt", "entry_point"}
    assert set(final) == {*visible, "test"}
    assert task.visible_argv[4] != task.final_argv[4]
    task._final_task.unlink()
    (task.source_dir / "solution.py").write_text("def solve(x): return x + 1\n")
    assert tasks._evaluate(task.visible_argv, task.source_dir) == 1


def test_nonprimitive_candidate_return_cannot_execute_deserialization_hooks(tmp_path):
    task = tasks.load_tasks(prepare(tmp_path))[0]
    marker = tmp_path / "unsafe-hook"
    (task.source_dir / "solution.py").write_text(
        "class Result:\n    def __reduce__(self):\n        open(" + repr(str(marker)) + ", 'w').write('unsafe')\n"
        "def solve(x): return Result()\n")
    assert tasks._evaluate(task.visible_argv, task.source_dir) == 0
    assert not marker.exists()


def test_old_inprocess_protocol_cannot_be_loaded_for_payment(tmp_path):
    manifest = prepare(tmp_path)
    data = json.loads(manifest.read_text())
    data["schema_version"] = 1
    tasks._json(manifest, data)
    manifest.with_name("manifest.sha256").write_text(tasks._sha(manifest))
    with pytest.raises(tasks.TaskPreparationError, match="obsolete"):
        tasks.load_tasks(manifest)


def test_official_host_random_is_fixed_before_each_final_check(tmp_path):
    data = rows()
    data[0]["test"] = "import random\ndef check(candidate):\n    assert random.random() == 0.8444218515250481\n    assert candidate(7) == 8\n"
    task = tasks.load_tasks(prepare(tmp_path, data))[0]
    (task.source_dir / "solution.py").write_text("def solve(x): return x + 1\n")
    assert tasks._evaluate(task.final_argv, task.source_dir) == 1
    assert tasks._evaluate(task.final_argv, task.source_dir) == 1
