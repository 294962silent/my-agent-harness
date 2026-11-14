"""rsi-study-v3 wrapper: protocol freeze, action-family probes, export gates, progress.

Offline by default. Campaign execution drives production research_cli through
private subprocess argv lists only; this module never changes production files
and never performs provider calls.
"""
from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[2]
ROOT = Path('C:/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/effect-validation-20261114')
OLD = Path('C:/Users/MLL/Desktop/my-agent-harness/.git/codex-delivery/research-runtime-20261114')
STUDY = ROOT / 'rsi-study-v3'
IMAGE = 'sha256:896edfbcbf0c29ad15681617a8886cb9871b7f24c5fde4606114c8ee6bfe4c70'
GOAL_SHA = '01c24ad5c08be683578cde4bfa36d6ffa50c8ee3cdc7b12a2d3e9723903dc61b'
RANDOM_PLANNER_SEED = 20261114
DELIVERY_CONTRACT = 'artifact_budget_v1'

MODEL_ARGS = ['--provider', 'openai', '--model', 'deepseek-v4.1-flash',
              '--base-url', 'https://opencode.ai/inference/go/openai/v1',
              '--temperature', '0.2', '--provider-timeout', '180',
              '--max-output-tokens', '16384', '--context-budget', '64000',
              '--max-attempts', '3', '--attempt-max-turns', '4',
              '--delivery-contract', DELIVERY_CONTRACT,
              '--memory-mode', 'none', '--evaluation-timeout', '60',
              '--replay-budgets', '1', '2', '3']

DATASETS = {
    'airfoil': {'csv': OLD / 'datasets/airfoil.csv', 'target': 'sound_pressure',
                'strategy_split': 'train', 'seed': 23, 'rows': 1503,
                'official_sha256': '6d80f05069521d4d9168ab5935b24981813ffbaf9cc73ce4462a813dc52f6059'},
    'yacht': {'csv': ROOT / 'data/yacht.csv', 'target': 'resistance',
              'strategy_split': 'train', 'seed': 23, 'rows': 308,
              'official_sha256': 'ee405606d10bd4708aebed7104f07c24bcda7ee428a030004207859a20187b27'},
    'wine-white': {'csv': OLD / 'datasets/winequality-white.csv', 'target': 'quality',
                   'strategy_split': 'holdout', 'seed': 31, 'rows': 4898,
                   'official_sha256': 'd99ea899f7ea892b717e34b0b6eaca33244c52d0d038dc4fcb6c728a64842562'},
    'qsar': {'csv': ROOT / 'data/qsar.csv', 'target': 'lc50',
             'strategy_split': 'test', 'seed': 31, 'rows': 546,
             'official_sha256': '24b033e29e4fcf7f3eb14037ce76ac91463093981836883d2ff726283c451eb0'},
    'auto-mpg': {'csv': ROOT / 'data/auto-mpg-complete.csv', 'target': 'mpg',
                 'strategy_split': 'test', 'seed': 31, 'rows': 392,
                 'official_sha256': '3554d51ba59adb5d90f79d6357a9df65dd8244c796ef14a6099d02c88ed7a237'},
}

LIMITS_ONE_ROUND = {'max_requests': 24, 'max_tokens': 400000, 'max_seconds': 1200, 'max_evaluations': 5}
LIMITS_TRAIN = {'max_requests': 48, 'max_tokens': 800000, 'max_seconds': 2400, 'max_evaluations': 9}

ARMS = {
    'fixed-reference': {'schedule': 'rsi', 'parameters': {'max_width': 2, 'max_refine': 1},
                        'origin': 'untrained_default_same_rsi_scheduler'},
    'random': {'schedule': 'random', 'parameters': {'max_width': 2, 'max_refine': 1},
               'origin': 'untrained_default_seeded_exploration_order'},
    'trained-frozen': {'schedule': 'rsi_if_promoted_else_fixed',
                       'parameters': 'optimizer_selected_if_promoted_else_previous_manifest_policy',
                       'origin': 'frozen_from_c-airfoil-train_optimizer_finished_event'},
    'fixed-greedy': {'schedule': 'rsi', 'parameters': {'max_width': 1, 'max_refine': 0},
                     'origin': 'untrained_reduced_search_same_rsi_scheduler'},
}

CAMPAIGNS = [
    {'name': 'c-airfoil-train', 'dataset': 'airfoil', 'split': 'train', 'arm': 'rsi-training',
     'rounds': 2, 'schedule': 'rsi', 'width': 2, 'refine': 1, 'limits': LIMITS_TRAIN},
    {'name': 'c-yacht-train', 'dataset': 'yacht', 'split': 'train', 'arm': 'rsi-training',
     'rounds': 2, 'schedule': 'rsi', 'width': 2, 'refine': 1, 'limits': LIMITS_TRAIN},
    {'name': 'c-wine-val', 'dataset': 'wine-white', 'split': 'val', 'arm': 'rsi-holdout',
     'rounds': 1, 'schedule': 'rsi', 'width': 2, 'refine': 1, 'limits': LIMITS_ONE_ROUND},
    {'name': 'c-qsar-fixed', 'dataset': 'qsar', 'split': 'test', 'arm': 'fixed-reference',
     'rounds': 1, 'schedule': 'rsi', 'width': 2, 'refine': 1, 'limits': LIMITS_ONE_ROUND},
    {'name': 'c-qsar-random', 'dataset': 'qsar', 'split': 'test', 'arm': 'random',
     'rounds': 1, 'schedule': 'random', 'width': 2, 'refine': 1, 'limits': LIMITS_ONE_ROUND},
    {'name': 'c-qsar-frozen', 'dataset': 'qsar', 'split': 'test', 'arm': 'trained-frozen',
     'rounds': 1, 'schedule': 'rsi', 'width': 2, 'refine': 1, 'limits': LIMITS_ONE_ROUND,
     'policy_from': 'c-airfoil-train'},
    {'name': 'c-qsar-greedy', 'dataset': 'qsar', 'split': 'test', 'arm': 'fixed-greedy',
     'rounds': 1, 'schedule': 'rsi', 'width': 1, 'refine': 0, 'limits': LIMITS_ONE_ROUND},
    {'name': 'c-autompg-fixed', 'dataset': 'auto-mpg', 'split': 'test', 'arm': 'fixed-reference',
     'rounds': 1, 'schedule': 'rsi', 'width': 2, 'refine': 1, 'limits': LIMITS_ONE_ROUND},
    {'name': 'c-autompg-random', 'dataset': 'auto-mpg', 'split': 'test', 'arm': 'random',
     'rounds': 1, 'schedule': 'random', 'width': 2, 'refine': 1, 'limits': LIMITS_ONE_ROUND},
    {'name': 'c-autompg-frozen', 'dataset': 'auto-mpg', 'split': 'test', 'arm': 'trained-frozen',
     'rounds': 1, 'schedule': 'rsi', 'width': 2, 'refine': 1, 'limits': LIMITS_ONE_ROUND,
     'policy_from': 'c-airfoil-train'},
    {'name': 'c-autompg-greedy', 'dataset': 'auto-mpg', 'split': 'test', 'arm': 'fixed-greedy',
     'rounds': 1, 'schedule': 'rsi', 'width': 1, 'refine': 0, 'limits': LIMITS_ONE_ROUND},
]

TEST_ORDER = ['c-qsar-fixed', 'c-qsar-random', 'c-qsar-frozen', 'c-qsar-greedy',
              'c-autompg-fixed', 'c-autompg-random', 'c-autompg-frozen', 'c-autompg-greedy']


def file_sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')


def dataset_pins():
    pins = []
    for name, spec in DATASETS.items():
        digest = file_sha(spec['csv'])
        if digest != spec['official_sha256']:
            raise ValueError('dataset SHA differs from official manifest: ' + name)
        pins.append({'dataset': name, 'csv': str(spec['csv']), 'target': spec['target'],
                     'strategy_split': spec['strategy_split'], 'seed': spec['seed'],
                     'rows': spec['rows'], 'sha256': digest,
                     'within_task_split': '60/20/20 (dev_fraction=.2, final_fraction=.2)'})
    return pins


def action_family_probe(seed=31, width=2, depth=1, max_attempts=3):
    """Real RsiRuntime.planned_nodes comparison: fixed order vs seeded random order."""
    from my_coding_agent.rsi_branch import LocalBranchFactory
    from my_coding_agent.rsi_runtime import FrozenPolicy, ParentBudget, RsiRuntime, TrustedEvaluator

    class _Unused(TrustedEvaluator):
        def evaluate(self, workspace, budget, llm):
            raise NotImplementedError('planned_nodes probe never evaluates')

    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / 'source'
        source.mkdir()
        (source / 'train.py').write_text('probe\n', encoding='utf-8')
        factory = LocalBranchFactory(source, Path(tmp) / 'out', attempt_max_turns=1,
                                     context_budget=1000, allowed_tool_names=('read',),
                                     allowed_files=('train.py',))
        budget = ParentBudget(max_requests=1, max_tokens=1000, max_seconds=10, max_evaluations=1)
        policy = FrozenPolicy({'max_width': width, 'max_refine': depth})

        def plan(schedule):
            runtime = RsiRuntime(factory=factory, evaluator=_Unused(), budget=budget, policy=policy,
                                 max_attempts=max_attempts, schedule=schedule, seed=seed,
                                 task_id='action-family-probe', split='train')
            return [list(node) for node in runtime.planned_nodes()]

    fixed = plan('fixed')
    rsi = plan('rsi')
    random_first = plan('random')
    random_second = plan('random')
    return {'seed': seed, 'width': width, 'refine': depth, 'max_attempts': max_attempts,
            'fixed_order': fixed, 'rsi_order': rsi,
            'fixed_equals_rsi_same_node_planner': fixed == rsi,
            'random_seed_order': random_first,
            'random_deterministic': random_first == random_second,
            'coincides_with_fixed': random_first == fixed,
            'random_order_treatment_difference': random_first != fixed,
            'refinement_present_fixed': any(node[1] > 0 for node in fixed)}


def optimizer_grid_probe(budgets=(1, 2, 3), limit=4):
    """Production effective_candidates + expected_waves action-signature proof.

    budgets are the REAL optimize budgets (frozen replay_budgets), not a larger
    offline probe. Baseline is counted separately; distinctness is judged on
    node sequences (branch, attempt), never on parameter hashes alone.
    """
    from my_coding_agent.dream_rsi_contract import policy_identity, validate_trace
    from my_coding_agent.dream_rsi_optimizer import effective_candidates, expected_waves

    def cell(score):
        return {'evaluated': True, 'combined_score': score, 'n_valid': None, 'n_total': None,
                'valids': [], 'sol_times_ms': [], 'max_gaps': []}

    def make_trace(scores):
        b0a0, b1a0, b0a1, baseline = scores
        nodes = [
            {'id': 'b0_a0', 'branch': 0, 'attempt': 0, 'parent_id': None, 'path': 'x',
             'intent': 'probe', 'tags': [], 'status': 'done', 'raw': cell(b0a0)},
            {'id': 'b1_a0', 'branch': 1, 'attempt': 0, 'parent_id': None, 'path': 'x',
             'intent': 'probe', 'tags': [], 'status': 'done', 'raw': cell(b1a0)},
            {'id': 'b0_a1', 'branch': 0, 'attempt': 1, 'parent_id': 'b0_a0', 'path': 'x',
             'intent': 'probe', 'tags': [], 'status': 'done', 'raw': cell(b0a1)},
        ]
        trace = {'version': 1, 'n_problems': 1, 'baseline': cell(baseline), 'nodes': nodes,
                 'feedback_type': 'host_numeric'}
        validate_trace(trace)
        return trace

    traces = [make_trace((0.30, 0.55, 0.70, 0.25)), make_trace((0.40, 0.35, 0.80, 0.30))]
    candidates = effective_candidates(traces, list(budgets), limit, search_order='fixed', seed=23)
    coverage = []
    for candidate in candidates:
        for trace in traces:
            for budget in budgets:
                for wave in expected_waves(trace, candidate['parameters'], budget):
                    for node in wave:
                        coverage.append(any(m['branch'] == node['branch'] and m['attempt'] == node['attempt']
                                            for m in trace['nodes']))

    def node_signature(parameters, budget):
        return [[(n['branch'], n['attempt']) for n in wave]
                for trace in traces for wave in expected_waves(trace, parameters, budget)]

    per_budget = {}
    for budget in budgets:
        families = [('baseline(None)', node_signature(None, budget))]
        for candidate in candidates:
            families.append((str(candidate['parameters']), node_signature(candidate['parameters'], budget)))
        distinct = {json.dumps(sig, sort_keys=True) for _, sig in families}
        per_budget[budget] = {'families': families,
                              'n_distinct_signatures_including_baseline': len(distinct)}
    hashes = [candidate['policy_hash'] for candidate in candidates]
    signatures = [
        [node_signature(candidate['parameters'], budget) for budget in budgets]
        for candidate in candidates
    ]
    baseline_signature = [node_signature(None, budget) for budget in budgets]
    return {'budgets': list(budgets), 'requested_limit': limit,
            'effective_candidates': [
                {'parameters': candidate['parameters'], 'policy_hash': candidate['policy_hash']}
                for candidate in candidates],
            'n_effective_candidates_non_reference': len(candidates),
            'n_distinct_non_reference_signatures': len({json.dumps(sig, sort_keys=True) for sig in signatures}),
            'baseline_signature_duplicated_as_candidate': baseline_signature in signatures,
            'baseline_counted_separately': True,
            'baseline_hash': policy_identity(None),
            'baseline_duplicated_as_candidate': policy_identity(None) in hashes,
            'per_budget_action_signatures': per_budget,
            'all_wave_nodes_recorded_in_traces': all(coverage),
            'note': 'distinctness judged on executable node sequences from production '
                    'expected_waves; width2/refine1 is effective-identical to the baseline '
                    'signature on (2-branch,<=1-refinement) traces, so the supported '
                    'non-reference space is 3 families; baseline completes the comparison '
                    'and cannot be forced out; no fourth distinct non-reference family exists'}


def prepare_argv(campaign):
    spec = DATASETS[campaign['dataset']]
    limits = campaign['limits']
    argv = ['uv', 'run', '--no-sync', 'python', '-m', 'my_coding_agent.research_cli', 'prepare',
            str(STUDY / campaign['name']),
            '--csv', str(spec['csv']),
            '--target', spec['target'],
            '--task-root', str(STUDY / ('t-' + campaign['dataset'])),
            '--image', IMAGE,
            '--rounds', str(campaign['rounds']),
            '--split', campaign['split'],
            '--arm', campaign['arm'],
            '--seed', str(spec['seed']),
            *MODEL_ARGS,
            '--max-requests', str(limits['max_requests']),
            '--max-tokens', str(limits['max_tokens']),
            '--max-seconds', str(limits['max_seconds']),
            '--max-evaluations', str(limits['max_evaluations']),
            '--width', str(campaign['width']),
            '--refine', str(campaign['refine']),
            '--schedule', campaign['schedule']]
    if 'policy_from' in campaign:
        argv += ['--policy-from', str(STUDY / campaign['policy_from'])]
    if campaign['schedule'] == 'random':
        argv += ['--planner-seed', str(RANDOM_PLANNER_SEED)]
    return argv


def run_argv(campaign_name):
    return ['uv', 'run', '--no-sync', 'python', '-m', 'my_coding_agent.research_cli', 'run',
            str(STUDY / campaign_name), '--auth-provider', 'openai', '--auth-profile', 'default']


def final_argv(campaign_name):
    return ['uv', 'run', '--no-sync', 'python', '-m', 'my_coding_agent.research_cli', 'final',
            str(STUDY / campaign_name)]


def check_export_gate(campaign_name):
    """R3 gate: every committed round must carry a complete trace before export/optimize."""
    from my_coding_agent.dream_rsi_contract import validate_trace

    root = STUDY / campaign_name
    manifest = json.loads((root / 'campaign.json').read_text(encoding='utf-8'))
    rows = [json.loads(line) for line in
            (root / 'journal.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    committed = [row['payload'] for row in rows if row['kind'] == 'round_committed']
    issues = []
    for payload in committed:
        if payload.get('trace') is None:
            issues.append('round %d has no complete trace (invalid attempt or missing trace)' % payload['round'])
            continue
        try:
            trace_path = (root / payload['trace']).resolve()
            if not trace_path.is_relative_to(root.resolve()):
                raise ValueError('trace escaped campaign root')
            validate_trace(json.loads(trace_path.read_text(encoding='utf-8')))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            issues.append('round %d has invalid scored trace (%s)' % (payload['round'], type(exc).__name__))
    if len(committed) != manifest['rounds']:
        issues.append('committed rounds %d != planned %d' % (len(committed), manifest['rounds']))
    return {'campaign': campaign_name, 'campaign_id': manifest['campaign_id'],
            'rounds_committed': len(committed), 'planned_rounds': manifest['rounds'],
            'gate_pass': not issues, 'issues': issues}


def campaign_progress(campaign_name):
    root = STUDY / campaign_name
    rows = [json.loads(line) for line in
            (root / 'journal.jsonl').read_text(encoding='utf-8').splitlines() if line.strip()]
    states = [row['payload']['budget_state'] for row in rows if 'budget_state' in row['payload']]
    state = states[-1] if states else None
    committed = [row['payload'] for row in rows if row['kind'] == 'round_committed']
    kinds = [row['kind'] for row in rows]
    return {'campaign': campaign_name,
            'events': len(rows),
            'round_committed': len(committed),
            'final_finished': 'final_finished' in kinds,
            'budget': None if state is None else {
                'requests': len(state['records']),
                'settled': sum(1 for r in state['records'] if r['status'] == 'settled'),
                'used_tokens': sum(r['used_tokens'] or 0 for r in state['records'] if r['status'] == 'settled'),
                'held_tokens': sum(r['reserved_tokens'] for r in state['records'] if r['status'] != 'settled'),
                'evaluations': len(state['evaluations']),
                'blocked': state['blocked'],
                'active_seconds': state.get('active_seconds')}}


def write_progress(stage, **fields):
    payload = {'executor': 'DS-1 deepseek-v4.1-flash max', 'stage': stage,
               'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
               'study': 'rsi-study-v3', **fields}
    target = ROOT / 'ds1-progress.json'
    temporary = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    temporary.write_bytes(canonical(payload))
    temporary.replace(target)
    return payload


def freeze_protocol():
    probe = action_family_probe(seed=RANDOM_PLANNER_SEED)
    grid = optimizer_grid_probe()
    protocol = {
        'name': 'rsi-study-v3',
        'delivery_contract': DELIVERY_CONTRACT,
        'frozen_before_any_result': True,
        'planning_seeds': {'random_planner_seed': RANDOM_PLANNER_SEED,
            'dataset_seeds': {name: spec['seed'] for name, spec in DATASETS.items()},
            'selection': 'independent RNG seed fixed before model calls or scores; no reroll after action-signature collision'},
        'artifact_qualification': {
            'allowed_terminal': 'end_turn or exact max_turns limit_details with iterations=limit=observed=4',
            'closed_boundary': 'all provider usage settled, zero held reservations, current-attempt unique tool IDs paired, no cleanup errors',
            'artifact': 'static train.py singleton copied before host grading; source, submission, host code pin and exported checkpoint SHA must agree',
            'known_invalid': 'terminal failure node retained with eligible=false, selectable score=null and observed requests/tokens/evaluations; never raises reward or ceiling',
            'unknown_or_infrastructure': 'block whole study, retain reservation/evidence, no automatic retry',
            'historical_v2': 'old end_turn-only failure labels and original stop reasons remain unchanged',
        },
        'datasets': dataset_pins(),
        'arms': ARMS,
        'campaigns': CAMPAIGNS,
        'test_execution_order': TEST_ORDER,
        'campaign_ceilings_note': 'ceilings are cumulative per campaign; no independent per-round enforcement is claimed',
        'optimizer': {
            'real_run_via': 'campaign.optimize on c-airfoil-train (journal optimizer_finished event; campaign-local registry)',
            'budgets_real': [1, 2, 3],
            'budgets_offline_verification': [1, 2, 3],
            'budgets_note': 'real optimize and offline probe both use the frozen replay_budgets [1,2,3]; '
                            'no separate offline-only budget is mixed into claims',
            'max_candidates': 4,
            'search_order': 'fixed',
            'seed': 23,
            'objective': 'ratio_cost',
            'cost_weight': 0.05,
            'max_workers': 1,
            'max_seconds': 120,
            'grid_probe': grid,
            'dedup_note': 'effective_candidates deduplicates executable signatures with the baseline signature first; '
                          'baseline stays in selection and is preferred on ties',
        },
        'action_family_probe': probe,
        'scheduler_isolation': {
            'fixed_reference': 'untrained runtime policy, default 2/1, SAME rsi scheduler and selection logic as trained arm',
            'trained_frozen': 'frozen optimizer parameters via policy_from; rsi scheduler when promoted',
            'random': 'same parameters, seeded exploration-order change only',
            'greedy': 'untrained reduced search 1/0 on the same rsi scheduler',
            'no_arm_algorithm_hints_in_prompts': True,
        },
        'identifiability_rules': [
            'R1 unknown/inflight usage anywhere stops the whole study; no automatic retry',
             'R2 selected artifact or unchanged mean baseline must have complete host evidence; known invalid candidates remain unselectable terminal failures',
             'R3 export requires complete terminal-attempt traces for ALL train/val rounds, not all-success traces; otherwise preserve evidence and STOP optimizer',
            'R4 promotion only via strict independent Wine holdout gain; never forced',
            'R5 no promotion with parameter-equivalent policy -> same effective policy as the reference (rsi_runtime fixed and rsi share one node planner, proven by action_family_probe) -> learning effect unidentifiable, not a scheduler-contrast',
            'R6 frozen effective policy equal to reference (scheduler+parameters+action budget) -> learning effect unidentifiable',
            'R7 baseline wins are reported as-is; no forcing',
            'R8 final/test feedback never enters tuning or memory',
            'R9 container evaluations coordinated with DS-2 via ROOT/glm-coordination.json fields; no full pytest during local embedding',
             'R10 historical calibration/v1/v2 negative evidence preserved, never relabeled or merged into v3',
        ],
        'v2_to_v3_changes': [
             'new ROOT/rsi-study-v3, new source pins/manifests/goals; no reuse or relabel of v2 results',
             'explicit artifact_budget_v1 with exact quota-boundary delivery, static artifact host pin binding, known-invalid failure nodes and baseline-initialized masked replay; default end_turn_v1 unchanged',
             'all arms use 4 iterations per attempt, 16384 output, 64000 context, 180s timeout and zero retries',
             'random planner seed20261114 independent of unchanged dataset seeds; offline action signatures recorded before any score',
             'actual optimizer and offline probe remain [1,2,3], same candidate signature dedup and strict independent Wine holdout promotion',
             'ratio_cost remains evaluator-count proxy; identical request/token ceilings do not imply equal measured token efficiency',
        ],
        'study_totals': {'campaigns': 11, 'rounds_theoretical': 13, 'attempts_ceiling': 39,
                          'planned_attempts_upper_bound': 35, 'iteration_requests_upper_bound': 140,
                          'host_evaluations_structural_upper_bound': 56,
                         'requests_ceiling': 312, 'tokens_ceiling': 5200000,
                         'evaluations_ceiling': 63, 'active_seconds_ceiling': 15600},
        'model_pins': {'model': 'deepseek-v4.1-flash', 'provider': 'openai',
                       'base_url': 'https://opencode.ai/inference/go/openai/v1',
                       'temperature': 0.2, 'timeout': 180, 'max_retries': 0,
                       'max_output_tokens': 16384, 'context_budget': 64000,
                        'attempt_max_turns': 4, 'max_attempts': 3,
                       'image': IMAGE, 'goal_sha': GOAL_SHA,
                       'memory': 'none'},
        'raw_rmse_note': 'host RMSE utility 1/(1+rmse) unchanged; raw RMSE comparable only within the same dataset/split; '
                          'report RMSE/baseline_RMSE against each task own frozen mean baseline alongside raw RMSE; do not average raw RMSE across target units',
        'cost_reporting': 'ratio_cost penalizes replay evaluation count only; report measured cumulative settled requests/input+output tokens/active time/evaluations separately; equal ceilings are not an actual-token efficiency claim; USD null unless verified',
    }
    digest = hashlib.sha256(canonical(protocol)).hexdigest()
    protocol['protocol_sha256'] = digest
    return protocol


def cli(argv):
    """Run a study subprocess with private argv (no shell); returns CompletedProcess."""
    return subprocess.run(argv, cwd=str(WORKTREE), capture_output=True, text=True, encoding='utf-8')
