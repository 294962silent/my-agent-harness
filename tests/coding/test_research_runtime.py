"""Durable campaign contracts, without network or Docker."""
import importlib.util
import json

import pytest


@pytest.fixture(autouse=True)
def clean_credentials(monkeypatch):
    import os
    for key in list(os.environ):
        if key.upper().endswith(('_API_KEY', '_ACCESS_TOKEN', '_AUTH_TOKEN')) or key in {
            'GOOGLE_APPLICATION_CREDENTIALS', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY', 'AZURE_CLIENT_SECRET'}:
            monkeypatch.delenv(key)


def test_production_runtime_exists():
    assert importlib.util.find_spec('my_coding_agent.research_runtime') is not None


def campaign(tmp_path):
    from my_coding_agent.research_runtime import ResearchCampaign
    from my_coding_agent.research_task import ResearchTask
    data = tmp_path / 'input.csv'
    data.write_text('x,y\n' + ''.join(f'{i},{i*2}\n' for i in range(20)))
    task = ResearchTask.prepare(data, tmp_path / 'task', target='y')
    return ResearchCampaign.prepare(task, tmp_path / 'campaign', image='sha256:' + 'a'*64,
        rounds=2, limits=dict(max_requests=8, max_tokens=100000, max_seconds=120,
                              max_evaluations=10, max_output_tokens=100))


def test_hash_chain_truncation_and_tamper_fail_closed(tmp_path):
    from my_coding_agent.research_runtime import ResearchCampaign, ResearchIntegrityError
    c = campaign(tmp_path)
    c.append('example', {'value': 1})
    path = c.root / 'journal.jsonl'
    original = path.read_bytes()
    path.write_bytes(original[:-1])
    with pytest.raises(ResearchIntegrityError):
        ResearchCampaign.load(c.root)
    path.write_bytes(original.replace(b'"value":1', b'"value":2'))
    with pytest.raises(ResearchIntegrityError):
        ResearchCampaign.load(c.root)


def test_reservation_survives_reload_and_blocks_replay(tmp_path):
    from my_coding_agent.research_runtime import ResearchCampaign, ResearchBudget, ResearchIntegrityError
    c = campaign(tmp_path)
    b = ResearchBudget(c)
    row = b.reserve([{'role': 'user', 'content': 'hello'}], None, 50, 'agent')
    assert c.events()[-1]['kind'] == 'request_reserved'
    restored = ResearchBudget(ResearchCampaign.load(c.root))
    assert restored.snapshot()['requests'] == 1
    assert restored.snapshot()['held_tokens'] == row['reserved_tokens']
    assert restored.blocked
    with pytest.raises(ResearchIntegrityError, match='unfinished'):
        c.ensure_ready()


def test_known_usage_and_active_time_accumulate(tmp_path):
    from my_coding_agent.research_runtime import ResearchBudget
    c = campaign(tmp_path)
    b = ResearchBudget(c)
    row = b.reserve([], None, 50, 'agent')
    b.settle(row, {'total_tokens': 40, 'completion_tokens': 10})
    b.persist('boundary')
    restored = ResearchBudget(c)
    assert restored.run.turns == 1
    assert restored.snapshot()['used_tokens'] == 40
    assert restored.snapshot()['held_tokens'] == 0
    assert restored.snapshot()['wall_seconds'] >= b.snapshot()['wall_seconds'] - 1
    assert restored.records[0]['input_tokens'] == 30
    assert restored.snapshot()['cost_usd'] is None


def test_started_round_blocks_new_objects(tmp_path):
    from my_coding_agent.research_runtime import ResearchCampaign, ResearchIntegrityError
    c = campaign(tmp_path)
    c.append('round_started', {'round': 0})
    with pytest.raises(ResearchIntegrityError, match='unfinished'):
        ResearchCampaign.load(c.root).ensure_ready()


def test_manifest_and_baseline_drift_block(tmp_path):
    from my_coding_agent.research_runtime import ResearchCampaign, ResearchIntegrityError
    c = campaign(tmp_path)
    (c.task.source_dir / 'train.py').write_text('tampered')
    with pytest.raises(ResearchIntegrityError):
        c.verify()
    manifest = c.root / 'campaign.json'
    value = json.loads(manifest.read_text())
    value['rounds'] = 99
    manifest.write_text(json.dumps(value))
    with pytest.raises(ResearchIntegrityError):
        ResearchCampaign.load(c.root)


def fake_evaluator():
    from my_coding_agent.rsi_runtime import TrustedEvaluator
    class Runner:
        async def verify_image(self):
            return 'sha256:' + 'a'*64
    class Evaluator(TrustedEvaluator):
        runner = Runner()
        feedback_type = 'host_rmse'
        final_calls = 0
        def verify(self):
            pass
        async def evaluate(self, workspace, budget, llm):
            budget.begin_evaluation().update(status='completed')
            return {'valid': True, 'score': .5, 'rmse': 1., 'feedback_type': self.feedback_type}
        async def evaluate_final(self, workspace, budget):
            self.final_calls += 1
            return await self.evaluate(workspace, budget, None)
    return Evaluator()


def scripted_raw():
    from my_agent_llm import Config, Response
    class Raw:
        config = Config(model='scripted-contract', max_tokens=64, max_retries=0)
        model = 'scripted-contract'
        calls = 0
        async def achat(self, messages, **kwargs):
            self.calls += 1
            return Response(model=self.model, content='done', usage={'total_tokens': 12, 'completion_tokens': 2})
    return Raw()


def test_real_agent_round_boundary_resume_and_independent_final(tmp_path):
    import asyncio
    from my_coding_agent.research_runtime import ResearchCampaign, ResearchIntegrityError
    c = campaign(tmp_path)
    raw = scripted_raw()
    first = asyncio.run(c.run(raw, pause_after_round=1, evaluator=fake_evaluator()))
    assert first['completed_rounds'] == 1
    assert first['budget']['requests'] == 2
    restored = ResearchCampaign.load(c.root)
    second = asyncio.run(restored.run(raw, evaluator=fake_evaluator()))
    assert second['completed_rounds'] == 2
    assert second['budget']['requests'] == 4
    assert second['budget']['used_tokens'] == 48
    assert second['budget']['evaluations'] == 6
    memory_before = {p.name: p.read_bytes() for p in (c.root / 'experience').iterdir() if p.is_file()}
    final_eval = fake_evaluator()
    result = asyncio.run(restored.final(evaluator=final_eval))
    assert result['score'] == .5 and final_eval.final_calls == 1
    assert raw.calls == 4
    assert memory_before == {p.name: p.read_bytes() for p in (c.root / 'experience').iterdir() if p.is_file()}
    with pytest.raises(ResearchIntegrityError, match='closed'):
        asyncio.run(ResearchCampaign.load(c.root).run(raw, evaluator=fake_evaluator()))


def test_unknown_usage_is_durable_and_never_replayed(tmp_path):
    from my_coding_agent.research_runtime import ResearchBudget
    from my_coding_agent.rsi_runtime import RsiBudgetExceeded
    c = campaign(tmp_path)
    b = ResearchBudget(c)
    row = b.reserve([], None, 50, 'agent')
    with pytest.raises(RsiBudgetExceeded):
        b.settle(row, None, error='CancelledError')
    restored = ResearchBudget(c)
    assert restored.blocked and restored.snapshot()['held_tokens'] > 0
    assert restored.records[0]['error_type'] == 'CancelledError'


def test_dirty_credentials_and_nonzero_retries_block_before_dispatch(tmp_path, monkeypatch):
    import asyncio
    from my_coding_agent.research_runtime import ResearchIntegrityError
    from my_agent_llm import Config
    c = campaign(tmp_path)
    raw = scripted_raw()
    monkeypatch.setenv('OPENAI_API_KEY', 'test-placeholder-never-real')
    with pytest.raises(ResearchIntegrityError, match='credential'):
        asyncio.run(c.run(raw, evaluator=fake_evaluator()))
    monkeypatch.delenv('OPENAI_API_KEY')
    raw.config = Config(model=raw.model, max_retries=3)
    with pytest.raises(ResearchIntegrityError, match='retry'):
        asyncio.run(c.run(raw, evaluator=fake_evaluator()))
    assert raw.calls == 0


def test_checkpoint_report_tamper_blocks_resume(tmp_path):
    import asyncio
    from my_coding_agent.research_runtime import ResearchCampaign, ResearchIntegrityError
    c = campaign(tmp_path)
    asyncio.run(c.run(scripted_raw(), pause_after_round=1, evaluator=fake_evaluator()))
    (c.root / c.selected()['report']).write_text('{}')
    with pytest.raises(ResearchIntegrityError):
        ResearchCampaign.load(c.root)


def test_cli_prepares_and_reports_without_auth_or_docker(tmp_path, capsys):
    assert importlib.util.find_spec('my_coding_agent.research_cli') is not None
    from my_coding_agent.research_cli import main
    data = tmp_path / 'input.csv'
    data.write_text('x,y\n' + ''.join(f'{i},{i*2}\n' for i in range(20)))
    root = tmp_path / 'campaign'
    assert main(['prepare', str(root), '--csv', str(data), '--target', 'y',
                 '--image', 'sha256:'+'a'*64, '--model', 'scripted-contract']) == 0
    prepared = json.loads(capsys.readouterr().out)
    assert prepared['completed_rounds'] == 0
    assert main(['status', str(root)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['ready'] and status['budget']['requests'] == 0


def test_optimizer_needs_independent_task_dataset(tmp_path):
    import asyncio
    c = campaign(tmp_path)
    asyncio.run(c.run(scripted_raw(), evaluator=fake_evaluator()))
    result = c.optimize()
    assert result['status'] == 'blocked' and not result['promoted']


def test_in_memory_policy_mutation_blocks_before_provider(tmp_path):
    from my_coding_agent.research_runtime import ResearchBudget, ResearchIntegrityError
    c = campaign(tmp_path)
    c.manifest['policy']['parameters']['max_width'] = 20
    with pytest.raises(ResearchIntegrityError):
        ResearchBudget(c).reserve([], None, 20, 'agent')


def test_dataset_export_uses_committed_traces_and_rejects_same_input_across_splits(tmp_path):
    import asyncio
    from my_coding_agent.research_runtime import ResearchCampaign, ResearchIntegrityError
    from my_coding_agent.research_task import ResearchTask
    from my_coding_agent.dream_rsi_optimizer import load_dataset
    train = campaign(tmp_path)
    asyncio.run(train.run(scripted_raw(), evaluator=fake_evaluator()))
    csv = tmp_path / 'holdout.csv'
    csv.write_text('z,target\n' + ''.join(f'{i},{i*3}\n' for i in range(20)))
    task = ResearchTask.prepare(csv, tmp_path/'holdout-task', target='target')
    holdout = ResearchCampaign.prepare(task, tmp_path/'holdout', image='sha256:'+'a'*64,
        rounds=1, split='val', limits=train.manifest['limits'])
    asyncio.run(holdout.run(scripted_raw(), evaluator=fake_evaluator()))
    exported = ResearchCampaign.export_dataset([train, holdout], tmp_path/'dataset')
    data = load_dataset(exported)
    assert len(data['entries']) == 3
    assert {e['split'] for e in data['entries']} == {'train', 'holdout'}
    assert all('task_evidence_sha256' in e for e in data['entries'])
    duplicate = ResearchCampaign.prepare(train.task, tmp_path/'duplicate', image='sha256:'+'a'*64,
        rounds=1, split='val', limits=train.manifest['limits'])
    asyncio.run(duplicate.run(scripted_raw(), evaluator=fake_evaluator()))
    with pytest.raises(ResearchIntegrityError, match='input'):
        ResearchCampaign.export_dataset([train, duplicate], tmp_path/'invalid-dataset')


def test_cli_owns_and_closes_http_clients():
    import asyncio
    from types import SimpleNamespace
    from my_coding_agent.research_cli import close_owned_llm
    calls = []
    class Sync:
        def close(self):
            calls.append('sync')
    class Async:
        async def close(self):
            calls.append('async')
    raw = SimpleNamespace(_provider=SimpleNamespace(client=Sync(), async_client=Async()))
    assert asyncio.run(close_owned_llm(raw))
    assert calls == ['async', 'sync']


@pytest.mark.parametrize('candidate_score,expected', [(.2, 'baseline'), (.5, 'baseline'), (.8, 'candidate')])
def test_selection_retains_baseline_for_worse_and_tied_candidates(tmp_path, candidate_score, expected):
    import asyncio
    from my_agent_llm import Response, ToolCall
    from my_coding_agent.rsi_runtime import _SCOPE
    c = campaign(tmp_path)
    raw = scripted_raw()
    base_type = type(raw)
    class EditingRaw(base_type):
        async def achat(self, messages, **kwargs):
            self.calls += 1
            tools = ([ToolCall(id='write-'+str(self.calls), name='write',
                args={'filePath': 'train.py', 'content': '# candidate\nprint(1)\n'})]
                if self.calls % 2 else None)
            return Response(model=self.model, content='done' if not tools else '', tool_calls=tools,
                            usage={'total_tokens': 12, 'completion_tokens': 2})
    evaluator = fake_evaluator()
    original = evaluator.evaluate
    async def scored(workspace, budget, llm):
        result = await original(workspace, budget, llm)
        result['score'] = .5 if _SCOPE.get().get('stage') == 'baseline' else candidate_score
        return result
    evaluator.evaluate = scored
    initial = (c.task.source_dir / 'train.py').read_bytes()
    asyncio.run(c.run(EditingRaw(), pause_after_round=1, evaluator=evaluator))
    selected = c.selected()
    assert selected['selection_source'] == expected
    assert selected['raw_agent_candidate_score'] == candidate_score
    code = (c.root / selected['checkpoint'] / 'train.py').read_bytes()
    assert (code == initial) == (expected == 'baseline')
    asyncio.run(c.run(EditingRaw(), evaluator=evaluator))
    assert (c.root / 'rounds/0001/baseline/train.py').read_bytes() == code


def test_final_rejects_resolved_image_identity_drift(tmp_path):
    import asyncio
    from my_coding_agent.research_runtime import ResearchIntegrityError
    c = campaign(tmp_path)
    asyncio.run(c.run(scripted_raw(), evaluator=fake_evaluator()))
    evaluator = fake_evaluator()
    class Runner:
        async def verify_image(self):
            return 'sha256:' + 'b'*64
    evaluator.runner = Runner()
    with pytest.raises(ResearchIntegrityError, match='image'):
        asyncio.run(c.final(evaluator=evaluator))
    assert evaluator.final_calls == 0


def test_prediction_artifact_drift_blocks_resume(tmp_path):
    import asyncio
    from my_coding_agent.research_runtime import ResearchCampaign, ResearchIntegrityError
    from my_coding_agent.rsi_branch import file_sha
    c = campaign(tmp_path)
    evaluator = fake_evaluator()
    original = evaluator.evaluate
    prediction = tmp_path / 'predictions.csv'
    prediction.write_text('id,prediction\n0,1\n')
    async def measured(workspace, budget, llm):
        result = await original(workspace, budget, llm)
        result['pins'] = {'code_sha256': file_sha(workspace/'train.py')}
        result['artifacts'] = {'predictions': str(prediction), 'predictions_sha256': file_sha(prediction)}
        return result
    evaluator.evaluate = measured
    asyncio.run(c.run(scripted_raw(), pause_after_round=1, evaluator=evaluator))
    prediction.write_text('tampered')
    with pytest.raises(ResearchIntegrityError):
        ResearchCampaign.load(c.root)


def test_optimizer_uses_frozen_ratio_cost_and_policy_snapshot(tmp_path, monkeypatch):
    import asyncio
    from my_coding_agent import dream_rsi_optimizer
    from my_coding_agent.research_runtime import ResearchCampaign
    c = campaign(tmp_path)
    asyncio.run(c.run(scripted_raw(), evaluator=fake_evaluator()))
    received = {}
    monkeypatch.setattr(dream_rsi_optimizer, 'load_dataset', lambda path: {'manifest_sha256': 'a'*64, 'kind': 'synthetic'})
    def optimize(dataset, output, **kwargs):
        received.update(kwargs)
        return {'selected': {'parameters': None, 'policy_hash': 'b'*64}, 'promoted': False}
    monkeypatch.setattr(dream_rsi_optimizer, 'optimize', optimize)
    c.optimize(tmp_path / 'dataset.json', dry_run=True)
    assert received['objective'] == 'ratio_cost' and received['cost_weight'] == .05
    assert received['search_order'] == 'fixed' and received['seed'] == 23
    assert received['budgets'] == [1, 2] and received['max_candidates'] == 4
    test = ResearchCampaign.prepare(c.task, tmp_path/'test', image=c.manifest['image'],
        split='test', policy_from=c.root)
    assert test.manifest['policy_origin']['execution_identity'] == 'fixed_reference_no_promotion'
    assert test.manifest['policy']['parameters'] == c.manifest['policy']['parameters']
    assert test.manifest['schedule'] == 'fixed'
