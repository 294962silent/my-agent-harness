"""Offline contracts for the compat3 cost-inheritance continuation."""
from copy import deepcopy
from pathlib import Path

import pytest

from experiments.research_validation import rsi_compat3
from experiments.research_validation import rsi_compat3_execute as driver
from my_coding_agent.rsi_branch import file_sha


def _current_source_pins_match():
    path = rsi_compat3.STUDY / 'protocol.json'
    if not path.exists():
        return False
    protocol = __import__('json').loads(path.read_text(encoding='utf-8'))
    return all(file_sha(Path(pin['path'])) == pin['sha256'] for pin in protocol['source_pins'])


def test_compat3_retains_parent_stop_usage_and_budget_without_expansion():
    verified = rsi_compat3.verify_parent()
    assert verified['stop_preserved'] and verified['late_readback_verified']
    assert verified['source_runtime_usage']['requests'] == 14
    assert verified['source_runtime_usage']['settled_tokens'] == 133196
    assert verified['source_runtime_usage']['evaluations'] == 4
    usage = rsi_compat3.HISTORICAL_USAGE
    assert usage['requests'] == 49
    assert usage['settled_tokens'] == 505594
    assert usage['accounting_units'] == 533356
    assert usage['evaluations'] == 16
    plan = rsi_compat3.budget_plan()
    assert plan['new_limits']['requests'] == 263
    assert plan['new_limits']['accounting_units'] == 4666644
    assert plan['new_limits']['evaluations'] == 47
    assert plan['new_limits']['active_seconds'] < 12145.924
    assert plan['combined_ceiling']['requests'] == 299
    assert plan['combined_ceiling']['accounting_units'] == 4800160
    assert plan['combined_ceiling']['evaluations'] == 63
    assert plan['combined_ceiling']['active_seconds'] <= rsi_compat3.JOINT_LIMITS['active_seconds']
    yacht = next(c for c in rsi_compat3.CONTINUATION_CAMPAIGNS if c['name'] == 'c-yacht-train')
    assert yacht['limits']['max_requests'] == 34
    assert yacht['limits']['max_tokens'] == 666804
    assert yacht['limits']['max_evaluations'] == 3


def test_prefix_import_excludes_uncommitted_requests_but_counts_their_cost():
    from my_coding_agent.research_continuation import verify_import_spec
    spec = rsi_compat3.yacht_import_spec()
    baseline = rsi_compat3.yacht_baseline_import_spec()
    assert spec['version'] == 2 and spec['mode'] == 'committed_prefix'
    assert spec['uncommitted_rounds'] == [1]
    assert len(spec['round_events']) == 1
    assert spec['source_runtime_usage']['requests'] == 14
    assert spec['source_runtime_usage']['settled_tokens'] == 133196
    assert baseline['round'] == 1 and baseline['source_evaluation'] == 4
    assert rsi_compat3.HISTORICAL_USAGE['compat2_settled_usage']['active_seconds'] > 0
    old, rounds = verify_import_spec(spec)
    assert len(rounds) == 1 and rounds[0]['payload']['round'] == 0
    assert old.manifest['campaign_id'] == spec['campaign_id']


def test_real_prefix_and_round1_baseline_import_are_fully_reverified(tmp_path):
    import asyncio
    import shutil
    from my_coding_agent.research_continuation import import_committed_campaign_prefix
    from my_coding_agent.research_runtime import ResearchBudget, ResearchCampaign
    from my_coding_agent.research_runtime import _JournalEvaluator
    from tests.coding.test_research_runtime import fake_evaluator
    yacht = next(c for c in rsi_compat3.CONTINUATION_CAMPAIGNS if c['name'] == 'c-yacht-train')
    frozen_protocol = (rsi_compat3.STUDY / 'protocol.json' if
                       (rsi_compat3.STUDY / 'protocol.json').exists() else
                       rsi_compat3.PARENT_STUDY / 'protocol.json')
    imported = import_committed_campaign_prefix(tmp_path / 'yacht', rsi_compat3.yacht_import_spec(),
        baseline_imports={'1': rsi_compat3.yacht_baseline_import_spec()}, limits=yacht['limits'],
        observation_contract='langfuse_campaign_v1',
        observation_protocol={'file': str(frozen_protocol), 'sha256': file_sha(frozen_protocol)})
    assert imported.ensure_ready() == 1
    assert imported.manifest['campaign_id'] != rsi_compat3.yacht_import_spec()['campaign_id']
    assert ResearchCampaign.load(imported.root).manifest['baseline_imports']['1']['source_evaluation'] == 4
    workspace = tmp_path / 'baseline'
    workspace.mkdir()
    shutil.copyfile(imported.root / imported.selected()['checkpoint'] / 'train.py',
                    workspace / 'train.py')
    budget = ResearchBudget(imported)
    evaluator = _JournalEvaluator(fake_evaluator(), budget,
        baseline_import=rsi_compat3.yacht_baseline_import_spec(), baseline_workspace=workspace)
    reused = asyncio.run(evaluator.evaluate(workspace, budget, None))
    assert reused['rmse'] == pytest.approx(16.662447071027422)
    assert budget.snapshot()['evaluations'] == budget.snapshot()['requests'] == 0


def test_compat3_protocol_is_minimal_and_readback_bound_is_at_least_180(tmp_path, monkeypatch):
    # This construction is offline and deliberately unrelated to model/provider credentials.
    protocol = rsi_compat3.build_protocol(driver.SOURCE_FILES, cli_command=driver.CLI_COMMAND,
                                          base_url='https://cloud.langfuse.com')
    assert protocol['version'] == 'rsi-study-v4-compat3'
    assert protocol['compatibility_parent_protocol_sha256'] == rsi_compat3.PARENT_PROTOCOL_SHA256
    assert protocol['observation']['readback_timeout_seconds'] >= 180
    assert protocol['observation']['max_readonly_queries_per_gate'] == 8
    assert protocol['observation']['readback_backoff_initial_seconds'] == 2
    assert protocol['yacht_history_import']['version'] == 2
    assert protocol['yacht_baseline_imports']['1']['source_evaluation'] == 4
    assert protocol['compatibility_amendment']['model_data_tasks_arms_coordinates_algorithm_unchanged'] is True


def test_new_unknown_or_exceeded_usage_always_fails_closed():
    row = {'requests': 0, 'used_tokens': 0, 'held_tokens': 0, 'evaluations': 0,
           'wall_seconds': 0, 'blocked': False, 'usage_complete': True}
    rsi_compat3.assert_joint_budget([row])
    bad = deepcopy(row)
    bad.update(blocked=True, usage_complete=False, held_tokens=1)
    with pytest.raises(RuntimeError, match='unknown'):
        rsi_compat3.assert_joint_budget([bad])
    too_many = deepcopy(row)
    too_many['evaluations'] = 48
    with pytest.raises(RuntimeError, match='ceiling'):
        rsi_compat3.assert_joint_budget([too_many])


def test_commands_use_fresh_root_and_final_is_after_freeze():
    for name in rsi_compat3.TRAIN_NAMES:
        run = rsi_compat3.run_argv(name)
        assert str(rsi_compat3.STUDY / name) in run
        assert all(str(rsi_compat3.PARENT_STUDY / name) != value for value in run)
        driver._command_kind(run, 'run')
    final = rsi_compat3.final_argv(rsi_compat3.TEST_ORDER[0])
    driver._command_kind(final, 'final')
    assert str(rsi_compat3.STUDY / rsi_compat3.TEST_ORDER[0]) in final


@pytest.mark.skipif(not _current_source_pins_match(),
                    reason='requires compat3 to have been frozen locally')
def test_compat3_offline_driver_smoke_has_no_paid_calls():
    evidence = driver.dry_run()
    assert evidence['new_paid_model_requests'] == 0
    assert evidence['new_evaluations'] == 0
    assert len(evidence['training_path']) == 3
    assert len(evidence['final_path']) == 8
