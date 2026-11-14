"""RSI continuation joint ceilings include irreversible historical occupancy."""
from copy import deepcopy

import pytest

from experiments.research_validation.rsi_continuation import (
    CONTINUATION_CAMPAIGNS, HISTORICAL_USAGE, JOINT_LIMITS, NEW_LIMITS,
    budget_plan, assert_joint_budget,
)


def test_joint_ceiling_keeps_unknown_occupancy_and_unavailable_actual_usage():
    plan = budget_plan()
    assert HISTORICAL_USAGE['settled_tokens'] == 369696
    assert HISTORICAL_USAGE['existing_reservation_occupancy'] == 27762
    assert plan['historical']['actual_total_tokens'] is None
    assert plan['historical']['requests'] == 33
    assert plan['new_campaign_ceilings'] == {'requests': 264, 'accounting_units': 4400000,
        'evaluations': 52, 'active_seconds': 13200}
    assert plan['combined_ceiling']['requests'] == 297
    assert plan['combined_ceiling']['accounting_units'] == 4797458
    assert plan['combined_ceiling']['evaluations'] == JOINT_LIMITS['evaluations'] == 63
    assert plan['combined_ceiling']['active_seconds'] <= JOINT_LIMITS['active_seconds']
    assert NEW_LIMITS['accounting_units'] == 4802542
    assert next(c for c in CONTINUATION_CAMPAIGNS if c['name'] == 'c-yacht-train')['limits']['max_evaluations'] == 8
    assert next(c for c in CONTINUATION_CAMPAIGNS if c['name'] == 'c-wine-val')['limits']['max_evaluations'] == 4


def test_budget_validation_never_refunds_history_and_stops_new_unknown():
    assert_joint_budget([])
    known = {'requests': 2, 'used_tokens': 12, 'held_tokens': 0, 'evaluations': 1,
             'wall_seconds': 4, 'blocked': False, 'usage_complete': True}
    summary = assert_joint_budget([known])
    assert summary['combined']['requests'] == 35
    assert summary['combined']['accounting_units'] == 397470
    assert summary['combined']['actual_total_tokens'] is None
    bad = deepcopy(known)
    bad.update(held_tokens=1, usage_complete=False, blocked=True)
    with pytest.raises(RuntimeError, match='unknown'):
        assert_joint_budget([bad])


@pytest.mark.parametrize('field,value', [('requests', 280), ('used_tokens', 4802543),
    ('evaluations', 53), ('wall_seconds', 13328)])
def test_new_budget_cannot_exceed_authorized_remaining(field, value):
    row = {'requests': 0, 'used_tokens': 0, 'held_tokens': 0, 'evaluations': 0,
           'wall_seconds': 0, 'blocked': False, 'usage_complete': True}
    row[field] = value
    with pytest.raises(RuntimeError, match='ceiling'):
        assert_joint_budget([row])


def test_continuation_argv_preserves_science_and_uses_only_new_study_paths():
    from experiments.research_validation import rsi_continuation as new
    from experiments.research_validation import rsi_study as old
    assert old.LIMITS_ONE_ROUND['max_evaluations'] == 5
    assert old.LIMITS_TRAIN['max_evaluations'] == 9
    for c in new.CONTINUATION_CAMPAIGNS:
        argv = new.prepare_argv(c)
        assert str(new.OLD_STUDY) not in ' '.join(argv)
        assert argv[argv.index('--observation-contract')+1] == 'langfuse_campaign_v1'
        assert argv[argv.index('--attempt-max-turns')+1] == '4'
        assert argv[argv.index('--context-budget')+1] == '64000'
        assert argv[argv.index('--memory-mode')+1] == 'none'
        if c['schedule'] == 'random':
            assert argv[argv.index('--planner-seed')+1] == '20261114'
            assert argv[argv.index('--seed')+1] == '31'


def test_only_complete_imported_rounds_are_eligible_for_export(tmp_path, monkeypatch):
    import asyncio
    from my_coding_agent.research_runtime import credential_environment
    for name in credential_environment():
        monkeypatch.delenv(name)
    from tests.coding.test_research_continuation import frozen_history
    from tests.coding.test_research_runtime import fake_evaluator, scripted_raw
    from my_coding_agent.research_continuation import import_completed_campaign
    from my_coding_agent.research_runtime import ResearchCampaign
    from my_coding_agent.research_task import ResearchTask
    old, spec = frozen_history(tmp_path)
    new = import_completed_campaign(tmp_path / 'imported', spec)
    data = tmp_path / 'holdout.csv'
    data.write_text('x,y\n' + ''.join(f'{i},{i*3}\n' for i in range(25)))
    task = ResearchTask.prepare(data, tmp_path / 'holdout-task', target='y')
    holdout = ResearchCampaign.prepare(task, tmp_path / 'holdout-c', rounds=1, split='val',
        image=old.manifest['image'], limits=old.manifest['limits'])
    asyncio.run(holdout.run(scripted_raw(), evaluator=fake_evaluator()))
    path = ResearchCampaign.export_dataset([new, holdout], tmp_path / 'dataset')
    import json
    entries = json.loads(path.read_text())['entries']
    assert len(entries) == 3
    for e in entries[:2]:
        assert 'imported_source_campaign=' + old.manifest['campaign_id'] in e['provenance']
        assert 'campaign=' + new.manifest['campaign_id'] in e['provenance']
    assert entries[-1]['split'] == 'holdout'
