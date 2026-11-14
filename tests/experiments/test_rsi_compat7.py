"""Offline contracts for compat7's zero-paid retry boundary."""
import pytest

from experiments.research_validation import rsi_compat7 as study
from experiments.research_validation import rsi_compat7_execute as driver


def test_compat7_parent_is_zero_paid_and_keeps_remaining_arms():
    verified = study.verify_parent()
    assert verified['parent_paid_requests'] == 0
    assert verified['parent_paid_evaluations'] == 0
    assert set(verified['parent_usage']) == set(study.REMAINING_NAMES)
    assert tuple(c['name'] for c in study.TEST_CAMPAIGNS) == study.REMAINING_NAMES


def test_compat7_protocol_uses_generic_compat_version_loader():
    protocol = study.build_protocol(driver.SOURCE_FILES, cli_command=driver.CLI_COMMAND,
                                    base_url='https://cloud.langfuse.com')
    assert protocol['version'] == 'rsi-study-v4-compat7'
    assert protocol['observation']['readback_timeout_seconds'] >= 180
    assert protocol['historical_verification']['parent_paid_requests'] == 0
    # The loader accepts numeric compat suffixes without a paid probe.
    assert protocol['version'].removeprefix('rsi-study-v4-compat').isdigit()


def test_compat7_commands_target_fresh_paths():
    for name in study.TEST_ORDER:
        for argv, kind in ((study.run_argv(name), 'run'), (study.final_argv(name), 'final')):
            assert str(study.STUDY / name) in argv
            driver.base._command_kind(argv, kind)


@pytest.mark.skipif(not (study.STUDY / 'protocol.json').exists(),
                    reason='requires compat7 freeze')
def test_compat7_offline_smoke():
    evidence = driver.base.dry_run()
    assert evidence['new_paid_model_requests'] == 0
    assert len(evidence['campaigns']) == 5
