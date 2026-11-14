"""Offline contracts for the post-unknown test-only compat4 subset."""
from copy import deepcopy
from pathlib import Path

import pytest

from experiments.research_validation import rsi_compat4 as study
from experiments.research_validation import rsi_compat4_execute as driver
from my_coding_agent.rsi_branch import file_sha


def _current_source_pins_match():
    path = study.STUDY / 'protocol.json'
    if not path.exists():
        return False
    protocol = __import__('json').loads(path.read_text(encoding='utf-8'))
    return all(file_sha(Path(pin['path'])) == pin['sha256'] for pin in protocol['source_pins'])


def test_compat4_inherits_both_unknown_requests_and_exact_compat3_usage():
    verified = study.verify_parent()
    assert verified['compat3_stop_preserved'] and verified['wine_round_uncommitted_unknown']
    assert verified['unknown_reservation_tokens'] == 27818
    usage = study.HISTORICAL_USAGE
    assert usage['unknown_requests'] == 2
    assert usage['settled_tokens'] == 746600
    assert usage['accounting_units'] == 802180
    assert usage['evaluations'] == 22
    plan = study.budget_plan()
    assert plan['new_campaign_ceilings'] == {'requests': 192,
        'accounting_units': 3200000, 'evaluations': 40, 'active_seconds': 9600}
    assert plan['combined_ceiling']['requests'] == 263
    assert plan['combined_ceiling']['accounting_units'] == 4002180
    assert plan['combined_ceiling']['evaluations'] == 62
    assert plan['combined_ceiling']['active_seconds'] <= study.JOINT_LIMITS['active_seconds']


def test_compat4_keeps_all_eight_coordinates_and_explicit_frozen_fallback():
    names = [campaign['name'] for campaign in study.TEST_CAMPAIGNS]
    assert names == study.TEST_ORDER
    assert len(names) == 8
    frozen = [campaign for campaign in study.TEST_CAMPAIGNS if campaign['name'].endswith('-frozen')]
    assert len(frozen) == 2
    assert all('policy_from' not in campaign for campaign in frozen)
    assert all(campaign['fallback_policy_origin'].startswith('fixed_reference') for campaign in frozen)
    decision = study.optimizer_decision()
    assert decision['optimizer_invoked'] is False and decision['promoted'] is False
    assert decision['frozen_before_any_compat4_test_result'] is True


def test_compat4_protocol_has_no_retry_and_long_readback():
    protocol = study.build_protocol(driver.SOURCE_FILES, cli_command=driver.CLI_COMMAND,
                                    base_url='https://cloud.langfuse.com')
    assert protocol['version'] == 'rsi-study-v4-compat4'
    assert protocol['observation']['readback_timeout_seconds'] >= 180
    assert protocol['observation']['provider_retries'] == 0
    assert protocol['historical_verification']['unknown_request'] == 10
    assert protocol['optimizer_decision']['promoted'] is False
    assert 'no Wine model request or attempt identity is replayed' in protocol['new_vs_parent']


def test_compat4_unknown_or_over_budget_still_fails_closed():
    good = {'requests': 0, 'used_tokens': 0, 'held_tokens': 0, 'evaluations': 0,
            'wall_seconds': 0, 'blocked': False, 'usage_complete': True}
    study.assert_joint_budget([good])
    bad = deepcopy(good)
    bad.update(blocked=True, usage_complete=False, held_tokens=1)
    with pytest.raises(RuntimeError, match='unknown'):
        study.assert_joint_budget([bad])
    over = deepcopy(good)
    over['used_tokens'] = study.NEW_LIMITS['accounting_units'] + 1
    with pytest.raises(RuntimeError, match='ceiling'):
        study.assert_joint_budget([over])


def test_compat4_commands_target_fresh_study_and_final_follows_freeze():
    for name in study.TEST_ORDER:
        run, final = study.run_argv(name), study.final_argv(name)
        assert str(study.STUDY / name) in run and str(study.STUDY / name) in final
        driver._command_kind(run, 'run')
        driver._command_kind(final, 'final')


@pytest.mark.skipif(not _current_source_pins_match(),
                    reason='requires compat4 local freeze')
def test_compat4_offline_driver_smoke():
    evidence = driver.dry_run()
    assert evidence['new_paid_model_requests'] == 0
    assert evidence['new_evaluations'] == 0
    assert len(evidence['campaigns']) == 8
