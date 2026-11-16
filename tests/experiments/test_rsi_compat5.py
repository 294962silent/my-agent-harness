"""Offline contracts for the zero-paid compat5 retry boundary."""
from pathlib import Path

import pytest

from experiments.research_validation import rsi_compat5 as study
from experiments.research_validation import rsi_compat5_execute as driver
from my_coding_agent.rsi_branch import file_sha


def _current_source_pins_match():
    path = study.STUDY / 'protocol.json'
    if not path.exists():
        return False
    protocol = __import__('json').loads(path.read_text(encoding='utf-8'))
    return all(file_sha(Path(pin['path'])) == pin['sha256'] for pin in protocol['source_pins'])


def test_compat5_parent_has_zero_paid_usage_and_all_eight_preparations():
    verified = study.verify_parent()
    assert verified['parent_paid_requests'] == 0
    assert verified['parent_paid_evaluations'] == 0
    assert set(verified['parent_usage']) == set(study.TEST_ORDER)
    assert all(row['requests'] == 0 and row['tokens'] == 0 and row['held'] == 0
               for row in verified['parent_usage'].values())


def test_compat5_reuses_same_budget_and_explicit_fallback():
    assert study.HISTORICAL_USAGE == study.previous.HISTORICAL_USAGE
    assert study.budget_plan()['new_campaign_ceilings'] == {
        'requests': 192, 'accounting_units': 3200000,
        'evaluations': 40, 'active_seconds': 9600}
    assert study.optimizer_decision()['promoted'] is False
    assert len(study.TEST_CAMPAIGNS) == 8


def test_compat5_protocol_recognizes_new_version_and_readback():
    protocol = study.build_protocol(driver.SOURCE_FILES, cli_command=driver.CLI_COMMAND,
                                    base_url='https://cloud.langfuse.com')
    assert protocol['version'] == 'rsi-study-v4-compat5'
    assert protocol['observation']['readback_timeout_seconds'] >= 180
    assert protocol['historical_verification']['parent_paid_requests'] == 0
    assert protocol['compatibility_amendment']['new_model_requests_at_amendment'] == 0


def test_compat5_commands_target_fresh_paths():
    for name in study.TEST_ORDER:
        for values, kind in ((study.run_argv(name), 'run'), (study.final_argv(name), 'final')):
            assert str(study.STUDY / name) in values
            driver.base._command_kind(values, kind)


@pytest.mark.skipif(not _current_source_pins_match(),
                    reason='requires compat5 local freeze')
def test_compat5_offline_driver_smoke():
    evidence = driver.base.dry_run()
    assert evidence['new_paid_model_requests'] == 0
    assert len(evidence['campaigns']) == 8
