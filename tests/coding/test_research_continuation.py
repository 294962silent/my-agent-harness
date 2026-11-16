"""Explicit immutable history import; no paid provider or Docker."""
import asyncio
import json
from pathlib import Path
import shutil

import pytest

from my_coding_agent.research_runtime import ResearchCampaign, ResearchBudget, canonical
from tests.coding.test_research_runtime import campaign, fake_evaluator, scripted_raw


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    from my_coding_agent.research_runtime import credential_environment
    for name in credential_environment():
        monkeypatch.delenv(name)


def frozen_history(tmp_path):
    from my_coding_agent.rsi_branch import file_sha
    c = campaign(tmp_path)
    asyncio.run(c.run(scripted_raw(), evaluator=fake_evaluator()))
    archive = tmp_path / 'old-source'
    archive.mkdir()
    module_dir = Path(__import__('my_coding_agent.research_runtime', fromlist=['x']).__file__).parent
    pins = []
    for name in c.manifest['implementation']:
        origin = module_dir / name
        shutil.copyfile(origin, archive / name)
        pins.append({'path': str(origin), 'sha256': file_sha(origin), 'bytes': origin.stat().st_size})
    source_pins = tmp_path / 'old-source-pins.json'
    source_pins.write_bytes(canonical(pins))
    spec = {'version': 1, 'source_root': str(c.root), 'source_archive': str(archive),
            'source_pins': str(source_pins), 'manifest_sha256': file_sha(c.root / 'campaign.json'),
            'journal_sha256': file_sha(c.root / 'journal.jsonl'),
            'source_pins_sha256': file_sha(source_pins), 'campaign_id': c.manifest['campaign_id'],
            'round_events': [r['sha256'] for r in c.events() if r['kind'] == 'round_committed']}
    return c, spec


def frozen_prefix(tmp_path):
    from my_coding_agent.research_continuation import _budget_usage
    from my_coding_agent.rsi_branch import file_sha
    from my_coding_agent.rsi_runtime import execution_scope
    c = campaign(tmp_path)
    asyncio.run(c.run(scripted_raw(), evaluator=fake_evaluator(), pause_after_round=1))
    c.append('round_started', {'round': 1,
        'seed_sha256': file_sha(c.root / c.selected()['checkpoint'] / 'train.py')})
    budget = ResearchBudget(c)
    with execution_scope(round=1, branch=0, attempt=0, stage='attempt'):
        row = budget.reserve([], None, 50, 'agent')
        budget.settle(row, {'total_tokens': 40, 'completion_tokens': 10})
    budget.persist('round_boundary')
    archive = tmp_path / 'old-prefix-source'
    archive.mkdir()
    module_dir = Path(__import__('my_coding_agent.research_runtime', fromlist=['x']).__file__).parent
    pins = []
    for name in c.manifest['implementation']:
        origin = module_dir / name
        shutil.copyfile(origin, archive / name)
        pins.append({'path': str(origin), 'sha256': file_sha(origin), 'bytes': origin.stat().st_size})
    source_pins = tmp_path / 'old-prefix-source-pins.json'
    source_pins.write_bytes(canonical(pins))
    spec = {'version': 2, 'mode': 'committed_prefix', 'source_root': str(c.root),
            'source_archive': str(archive), 'source_pins': str(source_pins),
            'manifest_sha256': file_sha(c.root / 'campaign.json'),
            'journal_sha256': file_sha(c.root / 'journal.jsonl'),
            'source_pins_sha256': file_sha(source_pins), 'campaign_id': c.manifest['campaign_id'],
            'round_events': [r['sha256'] for r in c.events() if r['kind'] == 'round_committed'],
            'uncommitted_rounds': [1], 'source_runtime_usage': _budget_usage(c)}
    return c, spec


def test_import_carries_exact_checkpoint_baseline_and_trace_with_distinct_provenance(tmp_path):
    from my_coding_agent.research_continuation import import_completed_campaign
    old, spec = frozen_history(tmp_path)
    old_journal = (old.root / 'journal.jsonl').read_bytes()
    new = import_completed_campaign(tmp_path / 'imported', spec)
    assert new.manifest['campaign_id'] != old.manifest['campaign_id']
    assert new.manifest['history_import'] == spec
    assert new.ensure_ready() == 2
    assert new.status()['imported_rounds'] == 2
    assert new.status()['new_completed_rounds'] == 0
    assert new.status()['budget']['requests'] == 0
    assert new.task.manifest_sha256 == old.task.manifest_sha256
    assert new.manifest['baseline_sha256'] == old.manifest['baseline_sha256']
    assert new.selected()['score'] == old.selected()['score']
    assert new.selected()['selection_source'] == 'baseline'
    assert (new.root / new.selected()['checkpoint'] / 'train.py').read_bytes() == (
        old.root / old.selected()['checkpoint'] / 'train.py').read_bytes()
    for item in new.events():
        if item['kind'] == 'round_imported':
            p = item['payload']
            assert p['history_origin']['source_campaign_id'] == old.manifest['campaign_id']
            assert (new.root / p['trace']).read_bytes() == (old.root / p['trace']).read_bytes()
            assert json.loads((new.root / p['report']).read_text())['baseline'] == json.loads(
                (old.root / p['report']).read_text())['baseline']
    raw = scripted_raw()
    assert asyncio.run(new.run(raw, evaluator=fake_evaluator()))['completed_rounds'] == 2
    assert raw.calls == 0
    assert (old.root / 'journal.jsonl').read_bytes() == old_journal
    assert ResearchCampaign.load(new.root).ensure_ready() == 2


@pytest.mark.parametrize('mutation', ['journal', 'manifest', 'source', 'trace', 'checkpoint', 'baseline', 'data'])
def test_import_rejects_every_frozen_material_drift(tmp_path, mutation):
    from my_coding_agent.research_continuation import import_completed_campaign
    from my_coding_agent.research_runtime import ResearchIntegrityError
    old, spec = frozen_history(tmp_path)
    p = old.selected()
    paths = {'journal': old.root / 'journal.jsonl', 'manifest': old.root / 'campaign.json',
             'source': Path(spec['source_archive']) / 'research_runtime.py',
             'trace': old.root / p['trace'], 'checkpoint': old.root / p['checkpoint'] / 'train.py',
             'baseline': old.task.source_dir / 'train.py', 'data': old.task.train_csv}
    with paths[mutation].open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ResearchIntegrityError):
        import_completed_campaign(tmp_path / 'imported', spec)
    assert not (tmp_path / 'imported').exists()


def test_unknown_or_partial_campaign_cannot_become_training_history(tmp_path):
    from my_coding_agent.research_continuation import import_completed_campaign
    from my_coding_agent.research_runtime import ResearchIntegrityError
    from my_coding_agent.rsi_branch import file_sha
    old, spec = frozen_history(tmp_path)
    b = ResearchBudget(old)
    b.reserve([], None, 50, 'agent')
    spec['journal_sha256'] = file_sha(old.root / 'journal.jsonl')
    with pytest.raises(ResearchIntegrityError):
        import_completed_campaign(tmp_path / 'imported', spec)
    assert not (tmp_path / 'imported').exists()


def test_import_rechecks_origin_and_never_resets_cost_on_reload(tmp_path):
    from my_coding_agent.research_continuation import import_completed_campaign
    from my_coding_agent.research_runtime import ResearchIntegrityError
    old, spec = frozen_history(tmp_path)
    new = import_completed_campaign(tmp_path / 'imported', spec)
    b = ResearchBudget(new)
    row = b.reserve([], None, 50, 'agent')
    b.settle(row, {'total_tokens': 40, 'completion_tokens': 10})
    restored = ResearchCampaign.load(new.root)
    assert ResearchBudget(restored).snapshot()['used_tokens'] == 40
    with (old.root / 'journal.jsonl').open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ResearchIntegrityError):
        ResearchCampaign.load(new.root)

def test_committed_prefix_import_is_separate_from_sunk_uncommitted_requests(tmp_path):
    from my_coding_agent.research_continuation import (
        import_committed_campaign_prefix, import_completed_campaign,
    )
    from my_coding_agent.research_runtime import ResearchIntegrityError
    old, spec = frozen_prefix(tmp_path)
    old_journal = (old.root / 'journal.jsonl').read_bytes()
    new = import_committed_campaign_prefix(tmp_path / 'prefix-import', spec)
    assert new.manifest['campaign_id'] != old.manifest['campaign_id']
    assert new.ensure_ready() == 1
    assert new.status()['imported_rounds'] == 1
    assert new.status()['budget']['requests'] == 0
    assert new.committed_rounds()[0]['payload']['history_origin']['source_event_sha256'] == spec['round_events'][0]
    assert (old.root / 'journal.jsonl').read_bytes() == old_journal
    with pytest.raises(ResearchIntegrityError, match='version 1'):
        import_completed_campaign(tmp_path / 'wrong-import', spec)
    raw = scripted_raw()
    asyncio.run(new.run(raw, evaluator=fake_evaluator()))
    assert new.ensure_ready() == 2
    assert new.status()['new_completed_rounds'] == 1
    assert ResearchBudget(new).snapshot()['requests'] == raw.calls
    with (old.root / 'journal.jsonl').open('ab') as stream:
        stream.write(b'changed')
    with pytest.raises(ResearchIntegrityError):
        ResearchCampaign.load(new.root)
