"""Entry-point regression for the second compatibility study."""
from pathlib import Path

from experiments.research_validation import rsi_compat as parent
from experiments.research_validation import rsi_compat2
from experiments.research_validation import rsi_compat2_execute as driver
from experiments.research_validation import rsi_continuation as scientific_parent
from my_coding_agent.rsi_branch import file_sha
import pytest


def _current_source_pins_match():
    path = rsi_compat2.STUDY / 'protocol.json'
    if not path.exists():
        return False
    protocol = __import__('json').loads(path.read_text(encoding='utf-8'))
    return all(file_sha(Path(pin['path'])) == pin['sha256'] for pin in protocol['source_pins'])


def test_compat2_exports_training_path_without_changing_science():
    assert rsi_compat2.TRAIN_NAMES == list(scientific_parent.TRAIN_NAMES)
    assert rsi_compat2.TEST_ORDER == list(parent.TEST_ORDER)
    assert rsi_compat2.NEW_LIMITS == parent.NEW_LIMITS
    assert rsi_compat2.CONTINUATION_CAMPAIGNS == parent.CONTINUATION_CAMPAIGNS
    assert rsi_compat2.budget_plan()['new_limits'] == parent.NEW_LIMITS


def test_compat2_run_and_final_commands_target_new_study():
    for name in rsi_compat2.TRAIN_NAMES:
        run = rsi_compat2.run_argv(name)
        assert str(rsi_compat2.STUDY / name) in run
        assert all(str(parent.STUDY / name) != arg for arg in run)
        driver._command_kind(run, 'run')
    name = rsi_compat2.TEST_ORDER[0]
    final = rsi_compat2.final_argv(name)
    assert str(rsi_compat2.STUDY / name) in final
    driver._command_kind(final, 'final')


def test_compat2_prepare_commands_use_fresh_campaign_paths():
    spec = rsi_compat2.CONTINUATION_CAMPAIGNS[0]
    argv = rsi_compat2.prepare_argv(spec)
    assert str(rsi_compat2.STUDY / spec['name']) in argv
    assert all(str(parent.STUDY / spec['name']) != arg for arg in argv)


@pytest.mark.skipif(not _current_source_pins_match(), reason='historical compat2 sources were superseded')
def test_compat2_protocol_retains_parent_science_and_zero_usage_boundary():
    protocol = rsi_compat2.build_protocol(
        driver.SOURCE_FILES, cli_command=driver.CLI_COMMAND,
        base_url='https://cloud.langfuse.com')
    assert protocol['version'] == 'rsi-study-v4-compat2'
    assert protocol['compatibility_parent_protocol_sha256'] == rsi_compat2.PARENT_PROTOCOL_SHA256
    assert protocol['budget_plan']['new_limits'] == parent.NEW_LIMITS
    assert protocol['historical_verification']['verified'] is True
    assert protocol['observation']['contract'] == 'langfuse_campaign_v1'
    assert protocol['old_v3_v4_compat1_permanently_preserved'] is True


@pytest.mark.skipif(not _current_source_pins_match(),
                    reason='requires the frozen local experiment receipt')
def test_compat2_prepared_driver_dry_run():
    evidence = driver.dry_run()
    assert evidence['status'] == 'PASS'
    assert evidence['new_paid_model_requests'] == 0
    assert len(evidence['training_path']) == 3
    assert len(evidence['final_path']) == 8
