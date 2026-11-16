"""Offline contracts for the remaining-arm compat6 continuation."""
from pathlib import Path

import pytest

from experiments.research_validation import rsi_compat6 as study
from experiments.research_validation import rsi_compat6_execute as driver
from my_coding_agent.rsi_branch import file_sha


def _current_source_pins_match():
    path = study.STUDY / 'protocol.json'
    if not path.exists():
        return False
    protocol = __import__('json').loads(path.read_text(encoding='utf-8'))
    return all(file_sha(Path(pin['path'])) == pin['sha256'] for pin in protocol['source_pins'])


def test_compat6_preserves_completed_arms_and_frozen_unknown():
    verified = study.verify_parent()
    assert verified['completed_arms'] == ['c-qsar-fixed', 'c-qsar-random']
    assert verified['failed_arm'] == 'c-qsar-frozen'
    usage = study.HISTORICAL_USAGE
    assert usage['requests'] == 107
    assert usage['unknown_requests'] == 3
    assert usage['accounting_units'] == 1342233
    assert usage['evaluations'] == 35
    assert usage['compat5_usage']['campaigns']['c-qsar-frozen']['reservation_occupancy'] == 87839


def test_compat6_runs_exactly_five_remaining_original_arms():
    assert tuple(campaign['name'] for campaign in study.TEST_CAMPAIGNS) == study.REMAINING_NAMES
    assert study.REMAINING_NAMES == ('c-qsar-greedy', 'c-autompg-fixed',
        'c-autompg-random', 'c-autompg-frozen', 'c-autompg-greedy')
    plan = study.budget_plan()
    assert plan['new_campaign_ceilings'] == {'requests': 120, 'accounting_units': 2000000,
        'evaluations': 25, 'active_seconds': 6000}
    assert plan['combined_ceiling']['requests'] == 227
    assert plan['combined_ceiling']['evaluations'] == 60
    assert plan['combined_ceiling']['active_seconds'] <= study.JOINT_LIMITS['active_seconds']


def test_compat6_protocol_has_no_replay_and_same_fallback():
    protocol = study.build_protocol(driver.SOURCE_FILES, cli_command=driver.CLI_COMMAND,
                                    base_url='https://cloud.langfuse.com')
    assert protocol['version'] == 'rsi-study-v4-compat6'
    assert protocol['historical_verification']['no_attempt_replay'] is True
    assert protocol['observation']['readback_timeout_seconds'] >= 180
    assert protocol['optimizer_decision']['promoted'] is False


def test_compat6_commands_target_fresh_paths():
    for name in study.TEST_ORDER:
        for argv, kind in ((study.run_argv(name), 'run'), (study.final_argv(name), 'final')):
            assert str(study.STUDY / name) in argv
            driver.base._command_kind(argv, kind)


@pytest.mark.skipif(not _current_source_pins_match(),
                    reason='requires compat6 freeze')
def test_compat6_offline_smoke():
    evidence = driver.base.dry_run()
    assert evidence['new_paid_model_requests'] == 0
    assert len(evidence['campaigns']) == 5
