"""Calibration contracts: no model, daemon, final labels or embedding execution."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace

import pytest

from experiments.research_validation import calibration as study
import asyncio
from pathlib import Path

from my_agent_llm import Config, Response
from my_coding_agent.dream_rsi_contract import validate_trace
from my_coding_agent.rsi_branch import LocalBranchFactory
from my_coding_agent.rsi_runtime import (
    FrozenPolicy,
    ParentBudget,
    RsiRuntime,
    TrustedEvaluator,
    _SCOPE,
)

from experiments.research_validation import rsi_study


@pytest.fixture
def prepared(tmp_path):
    csv = tmp_path / 'input.csv'
    csv.write_text('x,quality\n' + ''.join(f'{i},{i % 5}\n' for i in range(30)))
    return study.prepare(csv, tmp_path / 'calibration', image='sha256:' + 'a' * 64,
                         model='scripted-contract')


def test_prepare_freezes_production_calibration_without_dispatch(prepared):
    campaign = study.ResearchCampaign.load(prepared.root)
    assert study.verify_calibration(campaign) is campaign
    assert campaign.manifest['limits'] == study.LIMITS
    assert campaign.manifest['memory_snapshot'] == {}
    assert [e['kind'] for e in campaign.events()] == ['prepared']
    with pytest.raises(ValueError, match='both be new'):
        study.prepare('unused.csv', campaign.root, image='sha256:' + 'a' * 64, model='unused')


@pytest.mark.parametrize('key,value', [('memory_mode', 'lexical'), ('attempt_max_turns', 12),
                                      ('rounds', 2), ('limits', {'max_tokens': 999999})])
def test_run_refuses_unapproved_configuration_before_provider(prepared, key, value):
    plan = deepcopy(prepared.manifest)
    plan[key] = value
    with pytest.raises(ValueError, match='differs'):
        study.verify_calibration(SimpleNamespace(manifest=plan))


def test_run_uses_existing_production_entry_and_rejects_retry(prepared, monkeypatch):
    commands = []
    monkeypatch.setattr(study.research_cli, 'main', lambda argv: commands.append(argv) or 0)
    assert study.main(['run', str(prepared.root), '--auth-profile', 'host-only']) == 0
    assert commands == [['run', str(prepared.root), '--credential-env', 'RESEARCH_API_KEY',
                         '--auth-profile', 'host-only']]
    prepared.append('observation_started', {})
    with pytest.raises(ValueError, match='already started'):
        study.main(['run', str(prepared.root)])
    assert len(commands) == 1


def test_report_requires_normal_valid_changed_candidates_and_trace(prepared, monkeypatch):
    root = prepared.root / 'rounds/0000'
    (root / 'baseline').mkdir(parents=True)
    (root / 'baseline/train.py').write_bytes(b'baseline')
    attempts = []
    for i in range(2):
        checkpoint = root / f'attempt{i}'
        checkpoint.mkdir()
        (checkpoint / 'train.py').write_bytes(b'changed')
        (root / 'session-snapshot.jsonl').write_text(json.dumps({'id': 'length-message',
            'message': {'role': 'assistant', 'metadata': {'stop_reason': 'length'}}}) + '\n')
        attempts.append({'id': str(i), 'checkpoint': str(checkpoint), 'valid': True,
            'agent_terminal': {'stop_reason': 'end_turn'}, 'visible': {'score': .7, 'valid': True},
            'actions': [{'event': 'ToolStart', 'tool_name': 'write', 'tool_call_id': str(i)},
                        {'event': 'ToolEnd', 'tool_name': 'write', 'tool_call_id': str(i)}]})
    trace = b'{}'
    (root / 'trace.json').write_bytes(trace)
    result = {'status': 'completed', 'baseline': {'score': .5}, 'selected_visible_score': .7,
              'attempts': attempts, 'trace_sha256': hashlib.sha256(trace).hexdigest(), 'budget': {}}
    (root / 'result.json').write_text(json.dumps(result))
    monkeypatch.setattr(study.ResearchCampaign, 'load', lambda path: prepared)
    summary = study.report(prepared.root)['rounds'][0]
    assert summary['calibrated'] is True
    assert [a['length_count'] for a in summary['attempts']] == [1, 0]
    assert all(a['write_edit_calls'] == 1 and a['predictions_valid'] for a in summary['attempts'])
    assert all(a['code_sha256'] != summary['baseline_code_sha256'] for a in summary['attempts'])
    attempts[0]['valid'] = False
    attempts[0]['agent_terminal']['stop_reason'] = 'budget_exhausted'
    result['trace_sha256'] = None
    (root / 'result.json').write_text(json.dumps(result))
    summary = study.report(prepared.root)['rounds'][0]
    assert summary['calibrated'] is False and summary['complete_trace'] is False


def test_no_final_or_resume_command():
    for command in ('final', 'resume'):
        with pytest.raises(SystemExit):
            study.main([command, 'unused'])


class _StudyScores(TrustedEvaluator):
    """Offline scripted scoring of train.py strategy markers; zero provider calls."""

    async def evaluate(self, workspace, budget, llm):
        text = Path(workspace).joinpath('train.py').read_text(encoding='utf-8').strip()
        scores = {'STRATEGY_A': 0.3, 'STRATEGY_B': 0.6, 'STRATEGY_C': 0.9}
        if text in scores:
            return {'valid': True, 'score': scores[text], 'feedback_type': 'host_numeric'}
        return {'valid': False, 'score': None, 'feedback_type': 'host_numeric'}


class _StudyScripted:
    """Scripted model: one real write tool call then a concise end turn."""

    model = 'scripted-rsi-study-probe'

    def __init__(self):
        self.config = Config(provider='openai', model=self.model, max_tokens=64, max_retries=0)
        self.counts = {}

    async def achat(self, messages, tools=None, **kwargs):
        scope = _SCOPE.get()
        key = (scope.get('branch'), scope.get('attempt'))
        step = self.counts.get(key, 0)
        self.counts[key] = step + 1
        usage = {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}
        if step % 3 == 2:
            return Response(model=self.model, content='Submitted train.py; finished.',
                            usage=usage, finish_reason='stop')
        content = {(0, 0): 'STRATEGY_A', (1, 0): 'STRATEGY_B', (0, 1): 'STRATEGY_C'}.get(key, 'STRATEGY_A')
        return Response(model=self.model, content='', usage=usage, finish_reason='stop',
                        tool_calls=[{'id': 'w-%s-%s-%d' % (key[0], key[1], step), 'type': 'function',
                                     'function': {'name': 'write',
                                                  'arguments': json.dumps({'path': 'train.py', 'content': content})}}])


def test_study_action_families_refinement_selected(tmp_path):
    """Offline contract proof: real runtime + real branches + scripted evaluator;
    rsi 2/1 with max_attempts=3 dispatches a legal refinement (b0_a1) and the
    host can select it; the complete trace needs every attempt valid."""
    source = tmp_path / 'source'
    source.mkdir()
    (source / 'train.py').write_text('STRATEGY_A\n', encoding='utf-8')
    factory = LocalBranchFactory(source, tmp_path / 'rounds', attempt_max_turns=3,
                                 context_budget=64000,
                                 allowed_tool_names=('ls', 'find', 'read', 'write', 'edit'),
                                 allowed_files=('train.py',))
    budget = ParentBudget(max_requests=12, max_tokens=100000, max_seconds=120,
                          max_evaluations=6, max_output_tokens=4096)
    runtime = RsiRuntime(factory=factory, evaluator=_StudyScores(), budget=budget,
                         policy=FrozenPolicy({'max_width': 2, 'max_refine': 1}),
                         max_attempts=3, schedule='rsi', seed=31,
                         task_id='offline-action-family', split='train', arm='probe')
    result = asyncio.run(runtime.run(
        'Improve train.py with real tools; submit one complete candidate; then end turn.',
        llm=_StudyScripted()))
    report = result.report
    assert report['planned_attempts'] == 3
    assert [a['id'] for a in report['attempts']] == ['b0_a0', 'b1_a0', 'b0_a1']
    assert all(a['valid'] for a in report['attempts'])
    assert report['status'] == 'completed'
    assert report['selected_candidate'] == 'b0_a1'
    assert report['selected_visible_score'] == 0.9
    assert result.trace_path is not None
    trace = json.loads(result.trace_path.read_text(encoding='utf-8'))
    assert validate_trace(trace) == {'nodes': 3, 'branches': 2, 'max_attempt': 1}
    assert [(n['branch'], n['attempt']) for n in trace['nodes']] == [(0, 0), (1, 0), (0, 1)]
    plan = json.loads((factory.output / 'plan.json').read_text(encoding='utf-8'))
    assert [(n[0], n[1]) for n in plan['nodes']] == [(0, 0), (1, 0), (0, 1)]


def test_study_random_order_deterministic_and_coincidence_recorded():
    """Seed31 random plan comes from the real runtime planner; determinism is
    asserted and a coincidence with the fixed order is recorded, never rerolled."""
    probe = rsi_study.action_family_probe(seed=31)
    assert probe['random_deterministic'] is True
    assert probe['refinement_present_fixed'] is True
    assert len(probe['random_seed_order']) == 3
    assert probe['coincides_with_fixed'] in (True, False)
    assert probe['fixed_equals_rsi_same_node_planner'] is True
    assert probe['coincides_with_fixed'] is True
    assert probe['random_order_treatment_difference'] is False


def test_study_optimizer_grid_distinct_and_covered():
    """Production grid on synthetic (2-branch,<=1-refinement) traces: dedup yields
    three distinct non-reference families, no baseline duplicate, and every
    replayed wave node exists in the recorded traces (no invented outcomes)."""
    grid = rsi_study.optimizer_grid_probe(budgets=(1, 2, 3), limit=4)
    assert grid['n_effective_candidates_non_reference'] == 3
    assert grid['n_distinct_non_reference_signatures'] == 3
    assert grid['baseline_counted_separately'] is True
    assert grid['baseline_signature_duplicated_as_candidate'] is False
    pairs = {(c['parameters']['max_width'], c['parameters']['max_refine'])
             for c in grid['effective_candidates']}
    assert pairs == {(1, 1), (2, 0), (1, 0)}
    assert grid['baseline_duplicated_as_candidate'] is False
    assert grid['all_wave_nodes_recorded_in_traces'] is True
    assert grid['budgets'] == [1, 2, 3]


def test_study_export_gate_blocks_incomplete_trace(tmp_path, monkeypatch):
    """R3 gate: a committed round without a complete trace stops export/optimize."""
    root = tmp_path / 'campaign'
    root.mkdir()
    (root / 'campaign.json').write_text(json.dumps({'campaign_id': 'gate-test', 'rounds': 1}),
                                        encoding='utf-8')
    rows = [{'kind': 'round_committed', 'payload': {'round': 0, 'trace': None}}]
    (root / 'journal.jsonl').write_text('\n'.join(json.dumps(r) for r in rows) + '\n',
                                        encoding='utf-8')
    monkeypatch.setattr(rsi_study, 'STUDY', tmp_path)
    gate = rsi_study.check_export_gate('campaign')
    assert gate['gate_pass'] is False
    assert gate['issues'] and 'no complete trace' in gate['issues'][0]


def test_study_export_gate_rejects_unscored_trace(tmp_path, monkeypatch):
    root = tmp_path / 'campaign'
    root.mkdir()
    (root / 'campaign.json').write_text(json.dumps({'campaign_id': 'gate-test', 'rounds': 1}),
                                        encoding='utf-8')
    (root / 'trace.json').write_text('{}', encoding='utf-8')
    rows = [{'kind': 'round_committed', 'payload': {'round': 0, 'trace': 'trace.json'}}]
    (root / 'journal.jsonl').write_text('\n'.join(json.dumps(row) for row in rows) + '\n',
                                        encoding='utf-8')
    monkeypatch.setattr(rsi_study, 'STUDY', tmp_path)
    gate = rsi_study.check_export_gate('campaign')
    assert gate['gate_pass'] is False
    assert 'invalid scored trace' in gate['issues'][0]


def test_study_v3_freezes_actual_replay_budgets_and_matrix_totals(monkeypatch):
    monkeypatch.setattr(rsi_study, 'dataset_pins', lambda: [])
    protocol = rsi_study.freeze_protocol()
    assert protocol['name'] == 'rsi-study-v3'
    assert protocol['delivery_contract'] == 'artifact_budget_v1'
    assert protocol['model_pins']['attempt_max_turns'] == 4
    assert protocol['planning_seeds']['random_planner_seed'] == 20261114
    assert protocol['optimizer']['budgets_real'] == [1, 2, 3]
    assert protocol['optimizer']['grid_probe']['budgets'] == [1, 2, 3]
    argv = rsi_study.prepare_argv(rsi_study.CAMPAIGNS[0])
    start = argv.index('--replay-budgets') + 1
    assert argv[start:start + 3] == ['1', '2', '3']
    assert argv[argv.index('--delivery-contract')+1] == 'artifact_budget_v1'
    assert argv[argv.index('--attempt-max-turns')+1] == '4'
    totals = protocol['study_totals']
    assert totals['campaigns'] == len(rsi_study.CAMPAIGNS) == 11
    assert totals['rounds_theoretical'] == sum(row['rounds'] for row in rsi_study.CAMPAIGNS) == 13
    for limit, total in (('max_requests', 'requests_ceiling'), ('max_tokens', 'tokens_ceiling'),
                         ('max_evaluations', 'evaluations_ceiling'), ('max_seconds', 'active_seconds_ceiling')):
        assert totals[total] == sum(row['limits'][limit] for row in rsi_study.CAMPAIGNS)
    assert totals['planned_attempts_upper_bound'] == 35
    assert totals['iteration_requests_upper_bound'] == 140
    assert totals['host_evaluations_structural_upper_bound'] == 56


def test_study_v3_random_planner_seed_is_separate_and_preregistered():
    random = next(c for c in rsi_study.CAMPAIGNS if c['name'] == 'c-qsar-random')
    fixed = next(c for c in rsi_study.CAMPAIGNS if c['name'] == 'c-qsar-fixed')
    random_argv, fixed_argv = rsi_study.prepare_argv(random), rsi_study.prepare_argv(fixed)
    assert random_argv[random_argv.index('--seed')+1] == '31'
    assert random_argv[random_argv.index('--planner-seed')+1] == '20261114'
    assert '--planner-seed' not in fixed_argv
    probe = rsi_study.action_family_probe(seed=20261114)
    assert probe['random_deterministic'] is True
    assert probe['random_order_treatment_difference'] is True
