"""Pinned baseline reuse and sunk-cost accounting for one compatibility continuation."""
import asyncio
from copy import deepcopy
import csv
import json
from pathlib import Path
import shutil

import pytest

from my_coding_agent.research_continuation import verify_baseline_import
from my_coding_agent.research_runtime import (ResearchCampaign, ResearchBudget,
    ResearchIntegrityError, _JournalEvaluator, credential_environment)
from my_coding_agent.research_evaluator import score_predictions
from my_coding_agent.rsi_branch import file_sha
from tests.coding.test_research_runtime import campaign, fake_evaluator


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in credential_environment():
        monkeypatch.delenv(name)


def imported_baseline(tmp_path):
    old = campaign(tmp_path)
    output = old.root / 'evaluation'
    output.mkdir()
    predictions = output / 'predictions.csv'
    with predictions.open('w', newline='') as stream:
        writer = csv.writer(stream)
        writer.writerow(['id', 'prediction'])
        writer.writerows((key, value + 1) for key, value in old.task.labels('dev'))
    measured = {'valid': True, **score_predictions(predictions, old.task.labels('dev')),
        'feedback_type': 'host_rmse', 'feedback': 'offline host fixture',
        'pins': {'task_manifest_sha256': old.task.manifest_sha256,
            'code_sha256': old.manifest['baseline_sha256'], 'features_sha256': file_sha(old.task.dev_features),
            'image_id': old.manifest['image'], 'split': 'dev',
            'verifier_sha256': file_sha(Path(__import__('my_coding_agent.research_evaluator', fromlist=['x']).__file__)),
            'boundary_sha256': {name: file_sha(Path(__import__('my_coding_agent.research_runtime', fromlist=['x']).__file__).with_name(name))
                for name in ('research_task.py', 'rsi_branch.py')}},
        'artifacts': {'predictions': str(predictions), 'predictions_sha256': file_sha(predictions)}}
    budget = ResearchBudget(old)
    record = budget.begin_evaluation()
    record.update(status='completed', valid=True, stage='baseline', pins=measured['pins'])
    budget.persist('evaluation_finished')
    measurement = output / 'evaluation.json'
    report = output / 'report.json'
    measurement.write_text(json.dumps(measured))
    report.write_text(json.dumps({'baseline': measured}))
    archive = old.root / 'source-frozen'
    archive.mkdir()
    pins = []
    for index, name in enumerate(old.manifest['implementation']):
        source = Path(__import__('my_coding_agent.research_runtime', fromlist=['x']).__file__).with_name(name)
        shutil.copyfile(source, archive / f'{index:02d}-{name}')
        pins.append({'path': str(source), 'sha256': file_sha(source), 'bytes': source.stat().st_size})
    pin_file = old.root / 'source-pins.json'
    pin_file.write_text(json.dumps(pins))
    spec = {'version': 1, 'source_root': str(old.root), 'source_campaign_id': old.manifest['campaign_id'],
        'source_pins': str(pin_file), 'source_archive': str(archive), 'source_evaluation': 1,
        'report': str(report), 'measurement': str(measurement),
        'anchors': {str(path): file_sha(path) for path in
            (old.root / 'campaign.json', old.root / 'journal.jsonl', pin_file, report, measurement, predictions)}}
    new = ResearchCampaign.prepare(old.task, tmp_path / 'new', image=old.manifest['image'],
        limits=old.manifest['limits'], baseline_import=spec)
    return old, new, measured


def test_baseline_reuse_preserves_score_and_origin_without_new_evaluation(tmp_path):
    old, new, measured = imported_baseline(tmp_path)
    new.verify()
    workspace = tmp_path / 'baseline'
    workspace.mkdir()
    shutil.copyfile(new.task.source_dir / 'train.py', workspace / 'train.py')
    budget = ResearchBudget(new)
    evaluator = _JournalEvaluator(fake_evaluator(), budget, baseline_workspace=workspace)
    reused = asyncio.run(evaluator.evaluate(workspace, budget, None))
    assert reused == measured
    assert len(budget.evaluations) == 0
    event = next(e for e in new.events() if e['kind'] == 'baseline_evaluation_imported')
    assert event['payload']['source']['source_campaign_id'] == old.manifest['campaign_id']
    assert event['payload']['actual_new_evaluations'] == 0
    candidate = tmp_path / 'candidate'
    shutil.copytree(workspace, candidate)
    asyncio.run(evaluator.evaluate(candidate, budget, None))
    assert len(budget.evaluations) == 1
    assert len(ResearchBudget(ResearchCampaign.load(new.root)).evaluations) == 1


@pytest.mark.parametrize('mutation', ['journal', 'report', 'prediction', 'task', 'code', 'image', 'verifier', 'workspace'])
def test_baseline_reuse_rejects_changed_bindings(tmp_path, mutation):
    old, new, measured = imported_baseline(tmp_path)
    if mutation == 'journal':
        (old.root / 'journal.jsonl').write_bytes((old.root / 'journal.jsonl').read_bytes() + b' ')
    elif mutation == 'report':
        Path(new.manifest['baseline_import']['report']).write_text('{}')
    elif mutation == 'prediction':
        Path(measured['artifacts']['predictions']).write_text('id,prediction\nchanged,0\n')
    elif mutation in ('task', 'code', 'image', 'verifier'):
        key = {'task': 'task_manifest_sha256', 'code': 'baseline_sha256', 'image': 'image'}
        if mutation == 'task':
            new.task.manifest_sha256 = 'changed'
        elif mutation == 'verifier':
            new.manifest['baseline_import'] = deepcopy(new.manifest['baseline_import'])
            new.manifest['baseline_import']['source_evaluation'] = 2
        else:
            new.manifest[key[mutation]] = 'changed'
    workspace = None
    if mutation == 'workspace':
        workspace = tmp_path / 'changed'
        workspace.mkdir()
        (workspace / 'train.py').write_text('changed')
    with pytest.raises(ResearchIntegrityError, match='baseline'):
        verify_baseline_import(new, workspace=workspace)


def test_compat_ceiling_retains_both_old_studies_and_exact_51_new_evaluations():
    from experiments.research_validation.rsi_compat import budget_plan, assert_joint_budget
    plan = budget_plan()
    assert plan['historical']['requests'] == 35
    assert plan['historical']['settled_tokens'] == 372398
    assert plan['historical']['existing_reservation_occupancy'] == 27762
    assert plan['new_campaign_ceilings']['evaluations'] == 51
    assert plan['combined_ceiling']['evaluations'] == 63
    assert plan['new_limits']['requests'] == 277
    assert assert_joint_budget([])['combined']['actual_total_tokens'] is None


def test_missing_baseline_anchor_is_rejected(tmp_path):
    _, new, _ = imported_baseline(tmp_path)
    new.manifest['baseline_import']['anchors'] = {}
    with pytest.raises(ResearchIntegrityError, match='baseline'):
        verify_baseline_import(new)
