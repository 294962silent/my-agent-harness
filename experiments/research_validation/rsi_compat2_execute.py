"""Bounded compat2 executor; prepare and smoke are offline, execute pays explicitly."""
from __future__ import annotations

import argparse
import csv
import hashlib
from importlib.metadata import version as distribution_version
import json
import math
from pathlib import Path
import shutil
import statistics
import subprocess
import time

from filelock import FileLock
from experiments.research_validation import rsi_compat2 as study
from experiments.research_validation import rsi_observation
from my_coding_agent.research_continuation import import_completed_campaign
from my_coding_agent.research_runtime import ResearchCampaign, atomic_bytes, canonical
from my_coding_agent.rsi_branch import file_sha
from my_coding_agent.dream_rsi_contract import validate_trace
from my_coding_agent.dream_rsi_optimizer import effective_candidates, expected_waves

ROOT, STUDY, WORKTREE = study.ROOT, study.STUDY, study.WORKTREE
MODULES = ('research_runtime.py', 'research_cli.py', 'research_task.py', 'research_evaluator.py',
           'research_integrations.py', 'research_continuation.py', 'rsi_runtime.py', 'rsi_branch.py',
           'dream_rsi_contract.py', 'dream_rsi_optimizer.py', 'dream_rsi_registry.py', 'dream_rsi_replay.py',
           '_dream_rsi_worker.py')
SOURCE_FILES = [WORKTREE / 'src/my_coding_agent' / name for name in MODULES]
SOURCE_FILES += [WORKTREE / 'experiments/research_validation' / name for name in
                 ('rsi_study.py', 'rsi_continuation.py', 'rsi_observation.py', 'rsi_v4_execute.py',
                  'rsi_compat.py', 'rsi_compat_execute.py', 'rsi_compat2.py',
                  'rsi_compat2_execute.py')]
SOURCE_FILES += [WORKTREE / 'experiments/benchmark_live/observation.py']
SOURCE_FILES += [WORKTREE / 'src/my_agent_llm' / name for name in
                 ('observation.py', 'observation_runtime.py', 'observation_export.py', 'langfuse_export.py',
                  'client.py', 'providers/openai.py')]
CLI_COMMAND = ['D:/soft/node/node.exe',
    'C:/Users/MLL/AppData/Local/npm-cache/_npx/6a6af7828f1bc5cc/node_modules/langfuse-cli/bin/langfuse.mjs']


def save(path, value):
    atomic_bytes(path, canonical(value))


def source_check():
    protocol = json.loads((STUDY / 'protocol.json').read_text(encoding='utf-8'))
    for row in protocol['source_pins']:
        if file_sha(Path(row['path'])) != row['sha256']:
            raise RuntimeError('compat2 frozen source drift: ' + Path(row['path']).name)
    study.verify_parent()
    if protocol.get('compatibility_parent_evidence') != study.parent_evidence():
        raise RuntimeError('compat1 parent evidence changed after compat2 freeze')
    return protocol


def progress(stage, **fields):
    value = {'executor': 'DS-1 deepseek-v4.1-flash max', 'study': 'rsi-study-v4-compat2',
             'stage': stage, 'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
             **fields}
    save(ROOT / 'ds1-compat2-progress.json', value)
    save(STUDY / 'progress.json', value)
    print(json.dumps({'stage': stage, **fields}, ensure_ascii=False), flush=True)


def summaries():
    values, budgets = [], []
    for spec in [{'name': 'c-airfoil-train'}, *study.CONTINUATION_CAMPAIGNS]:
        path = STUDY / spec['name']
        if not (path / 'campaign.json').exists():
            continue
        c = ResearchCampaign.load(path)
        state, events = c.status(), c.events()
        budget = state['budget']
        persisted = [r['payload']['budget_state'] for r in events if 'budget_state' in r['payload']]
        budget['wall_seconds'] = persisted[-1]['active_seconds'] if persisted else 0
        budgets.append(budget)
        rounds = []
        mean_baseline = None
        for row in c.committed_rounds():
            p = row['payload']
            report = json.loads((c.root / p['report']).read_text(encoding='utf-8'))
            baseline = report['baseline']
            selected = baseline if p['selection_source'] == 'baseline' else next(
                a['visible'] for a in report['attempts'] if a['id'] == p['raw_agent_candidate_id'])
            rmse, seed_rmse = selected.get('rmse'), baseline.get('rmse')
            if mean_baseline is None:
                mean_baseline = seed_rmse
            rounds.append({'round': p['round'], 'origin': row['kind'],
                'history_origin': p.get('history_origin'),
                'round_seed_baseline_rmse': seed_rmse, 'mean_baseline_rmse': mean_baseline,
                'selected_rmse': rmse, 'utility': p['score'],
                'rmse_over_mean_baseline': rmse / mean_baseline if mean_baseline else None,
                'selection_source': p['selection_source'], 'code_sha256': p['code_sha256'],
                'trace_complete': p['trace'] is not None,
                'known_invalid_attempts': report['invalid_attempts']})
        final = state['final']
        if final is not None:
            with c.task.train_csv.open(encoding='utf-8', newline='') as stream:
                mean = statistics.mean(float(r[c.task.manifest['target']]) for r in csv.DictReader(stream))
            with (c.task.root / 'private/final_labels.csv').open(encoding='utf-8', newline='') as stream:
                labels = [float(r['target']) for r in csv.DictReader(stream)]
            baseline_final = math.hypot(*[(mean-v) / math.sqrt(len(labels)) for v in labels])
            final = {**final, 'mean_baseline_rmse': baseline_final,
                     'rmse_over_mean_baseline': final['rmse'] / baseline_final if baseline_final else None}
        values.append({'campaign': spec['name'], 'campaign_id': c.manifest['campaign_id'],
            'split': c.manifest['split'], 'arm': c.manifest['arm'], 'rounds': rounds,
            'planned_rounds': state['planned_rounds'], 'new_budget': budget, 'final': final,
            'remote_readback_samples': [e['payload'] for e in events
                                        if e['kind'] == 'observation_readback_verified'],
            'policy_origin': c.manifest['policy_origin']})
        kinds = [e['kind'] for e in events]
        save(STUDY / 'last-summary.json', values)
        if (kinds.count('round_started') != kinds.count('round_committed') or
                ('final_started' in kinds) != ('final_finished' in kinds) or
                ('optimizer_started' in kinds) != ('optimizer_finished' in kinds)):
            raise RuntimeError('unfinished work permanently stops compat2: ' + spec['name'])
    totals = study.assert_joint_budget(budgets)
    save(STUDY / 'joint-usage.json', totals)
    save(STUDY / 'last-summary.json', values)
    return {'campaigns': values, 'usage': totals}


def invoke(argv, label):
    source_check()
    run = subprocess.run(argv, cwd=WORKTREE, capture_output=True, text=True, encoding='utf-8')
    receipt = {'argv': argv, 'returncode': run.returncode,
        'stdout_sha256': hashlib.sha256(run.stdout.encode()).hexdigest(),
        'stderr_sha256': hashlib.sha256(run.stderr.encode()).hexdigest()}
    save(STUDY / 'command-evidence' / (label + '.json'), receipt)
    atomic_bytes(STUDY / 'command-evidence' / (label + '.stdout-private.txt'), run.stdout.encode())
    atomic_bytes(STUDY / 'command-evidence' / (label + '.stderr-private.txt'), run.stderr.encode())
    if run.returncode:
        raise RuntimeError('production command failed; preserve evidence: ' + label)


def verify_prepared(c, spec):
    m = c.manifest
    if (any(m['limits'].get(k) != v for k, v in spec['limits'].items()) or
            m['delivery_contract'] != 'artifact_budget_v1' or m['attempt_max_turns'] != 4 or
            m['optimization']['budgets'] != [1, 2, 3] or
            m.get('observation_contract') != 'langfuse_campaign_v1' or
            m['observation_protocol']['sha256'] != file_sha(STUDY / 'protocol.json') or
            m.get('planner_seed') != (20261114 if spec['schedule'] == 'random' else None)):
        raise RuntimeError('prepared campaign differs from frozen compat2 protocol')


def prepare_one(spec):
    path = STUDY / spec['name']
    if not (path / 'campaign.json').exists():
        if path.exists():
            raise RuntimeError('partial preparation preserved: ' + spec['name'])
        invoke(study.prepare_argv(spec), 'prepare-' + spec['name'])
    verify_prepared(ResearchCampaign.load(path), spec)


def bootstrap():
    if not STUDY.exists():
        protocol = study.build_protocol(SOURCE_FILES, cli_command=CLI_COMMAND,
                                        base_url='https://cloud.langfuse.com')
        STUDY.mkdir()
        save(STUDY / 'protocol.json', protocol)
        save(STUDY / 'source-pins.json', protocol['source_pins'])
        save(STUDY / 'baseline-import.json', protocol['baseline_import'])
        for index, pin in enumerate(protocol['source_pins']):
            atomic_bytes(STUDY / 'source-frozen' / f'{index:02d}-{Path(pin["path"]).name}',
                         Path(pin['path']).read_bytes())
        save(STUDY / 'carry-forward.json', {'historical_usage': study.HISTORICAL_USAGE,
             'old_anchors': study.OLD_ANCHORS, 'verification': protocol['historical_verification']})
    protocol = source_check()
    imported = STUDY / 'c-airfoil-train'
    if not imported.exists():
        import_completed_campaign(imported, protocol['history_import'])
    else:
        c = ResearchCampaign.load(imported)
        if (c.manifest.get('history_import') != protocol['history_import'] or
                c.status()['imported_rounds'] != 2):
            raise RuntimeError('Airfoil imported history differs')
    for spec in study.CONTINUATION_CAMPAIGNS[:2]:
        prepare_one(spec)
    progress('compat2_frozen_offline_prepared', protocol_sha256=file_sha(STUDY / 'protocol.json'),
             new_paid_requests=0, summary=summaries())


def export_gate(name):
    c = ResearchCampaign.load(STUDY / name)
    if c.ensure_ready() != c.manifest['rounds']:
        raise RuntimeError('complete train/holdout rounds required')
    for row in c.committed_rounds():
        p = row['payload']
        if not p['trace']:
            raise RuntimeError('incomplete terminal trace blocks optimizer')
        report = json.loads((c.root / p['report']).read_text(encoding='utf-8'))
        if file_sha(c.root / p['trace']) != report['trace_sha256']:
            raise RuntimeError('trace checksum changed')
        validate_trace(json.loads((c.root / p['trace']).read_text(encoding='utf-8')))


def actual_grid(dataset):
    entries = json.loads(dataset.read_text(encoding='utf-8'))['entries']
    traces = [(e['split'], json.loads((dataset.parent / e['file']).read_text(encoding='utf-8')))
              for e in entries]
    train = [trace for split, trace in traces if split == 'train']
    if len(train) != 4 or len(traces) != 5:
        raise RuntimeError('require four train and one independent holdout traces')
    candidates = effective_candidates(train, [1, 2, 3], 4, search_order='fixed', seed=23)
    visits = 0
    for _, trace in traces:
        validate_trace(trace)
        nodes = {(n['branch'], n['attempt']): n for n in trace['nodes']}
        for parameters in [None, *[c['parameters'] for c in candidates]]:
            for budget in [1, 2, 3]:
                for wave in expected_waves(trace, parameters, budget):
                    for n in wave:
                        node = nodes[(n['branch'], n['attempt'])]
                        cell = node['raw']
                        if not ((node['eligible'] and cell['evaluated'] is True and
                                 math.isfinite(cell['combined_score'])) or
                                (not node['eligible'] and cell == {'evaluated': False,
                                 'combined_score': None} and node['cost']['usage_complete'])):
                            raise RuntimeError('unscored/unknown replay node')
                        visits += 1
    save(STUDY / 'actual-grid.json', {'budgets': [1, 2, 3], 'candidates': candidates,
         'baseline_participates_separately': True, 'checked_node_visits': visits})


def _command_kind(argv, name):
    try:
        module_index = argv.index('my_coding_agent.research_cli')
    except ValueError as exc:
        raise RuntimeError('research CLI module entry missing') from exc
    if module_index + 1 >= len(argv) or argv[module_index + 1:module_index + 2] != [name]:
        raise RuntimeError(f'research CLI {name} entry missing')


def dry_run():
    """Exercise every Python entry and command shape without dispatching a provider."""
    protocol = source_check()
    if not shutil.which('uv'):
        raise RuntimeError('uv executable unavailable')
    if not study.TRAIN_NAMES or len(study.TEST_ORDER) != 8:
        raise RuntimeError('training/test entry inventory changed')
    observation = protocol['observation']
    if distribution_version('langfuse') != observation['sdk_version']:
        raise RuntimeError('frozen Langfuse SDK version changed')
    if any(file_sha(Path(path)) != digest for path, digest in observation['cli_pins'].items()):
        raise RuntimeError('frozen Langfuse CLI changed')
    langfuse_env = rsi_observation.load_observation_environment()
    base_url = langfuse_env.get('LANGFUSE_BASE_URL') or langfuse_env.get('LANGFUSE_HOST')
    if (not langfuse_env.get('LANGFUSE_PUBLIC_KEY') or
            not langfuse_env.get('LANGFUSE_SECRET_KEY') or
            (base_url or '').rstrip('/') != observation['base_url'].rstrip('/')):
        raise RuntimeError('required Langfuse configuration unavailable/mismatched')
    langfuse_env.clear()
    train_plan = []
    for name in study.TRAIN_NAMES:
        c = ResearchCampaign.load(STUDY / name)
        ready = c.ensure_ready()
        if ready < c.manifest['rounds']:
            rsi_observation.load_frozen_protocol(c)
        _command_kind(study.run_argv(name) + ['--pause-after-round', str(ready + 1)], 'run')
        train_plan.append({'campaign': name, 'ready_rounds': ready,
            'planned_rounds': c.manifest['rounds'], 'run_argv': study.run_argv(name),
            'export_gate_when_complete': True})
    test_plan = []
    by_name = {c['name']: c for c in study.CONTINUATION_CAMPAIGNS}
    for name in study.TEST_ORDER:
        prepare = study.prepare_argv(by_name[name])
        run = study.run_argv(name)
        final = study.final_argv(name)
        _command_kind(run, 'run')
        _command_kind(final, 'final')
        test_plan.append({'campaign': name, 'prepare_argv': prepare, 'run_argv': run,
                          'final_argv': final, 'final_after_frozen_policy': True})
    evidence = {'version': 1, 'status': 'PASS', 'new_paid_model_requests': 0,
        'new_evaluations': 0, 'protocol_sha256': file_sha(STUDY / 'protocol.json'),
        'source_manifest_sha256': file_sha(STUDY / 'source-pins.json'),
        'entry_symbols': {'TRAIN_NAMES': study.TRAIN_NAMES, 'TEST_ORDER': study.TEST_ORDER,
                          'ResearchCampaign': True, 'import_completed_campaign': True,
                          'validate_trace': True, 'effective_candidates': True},
        'training_path': train_plan, 'final_path': test_plan,
        'langfuse': {'required': True, 'credentials_present': True,
                     'base_url_matches_protocol': True, 'sdk_and_cli_pins_match': True},
        'budget_plan': protocol['budget_plan'], 'observation_contract': observation['contract'],
        'final_isolation': protocol['reporting']['final']}
    save(STUDY / 'driver-smoke.json', evidence)
    save(ROOT / 'ds1-compat2-driver-smoke.json', evidence)
    progress('offline_driver_dry_run_passed', new_paid_requests=0, new_evaluations=0,
             train_campaigns=len(train_plan), final_campaigns=len(test_plan))
    print(json.dumps({'status': 'PASS', 'new_paid_model_requests': 0,
                      'train_campaigns': len(train_plan), 'final_campaigns': len(test_plan)}))
    return evidence


def execute(boundary_seconds):
    if (STUDY / 'STOP.json').exists():
        raise RuntimeError('compat2 STOP is permanent; no automatic continuation')
    source_check()
    start = time.monotonic()
    def boundary():
        return time.monotonic() - start >= boundary_seconds
    for name in study.TRAIN_NAMES:
        c = ResearchCampaign.load(STUDY / name)
        for index in range(c.ensure_ready(), c.manifest['rounds']):
            if boundary():
                progress('safe_boundary', next_action='continue at next unstarted round', summary=summaries())
                return
            summaries()
            invoke(study.run_argv(name) + ['--pause-after-round', str(index + 1)],
                   f'run-{name}-round-{index}')
            export_gate(name) if index + 1 == c.manifest['rounds'] else None
            progress('training_round_complete', campaign=name, round=index, summary=summaries())
        export_gate(name)
    if boundary():
        progress('safe_boundary', next_action='export train/holdout traces', summary=summaries())
        return
    dataset = STUDY / 'optimizer-dataset/dataset.json'
    if not dataset.exists():
        ResearchCampaign.export_dataset([STUDY / n for n in study.TRAIN_NAMES], dataset.parent)
    actual_grid(dataset)
    train = ResearchCampaign.load(STUDY / 'c-airfoil-train')
    finished = [e['payload'] for e in train.events() if e['kind'] == 'optimizer_finished']
    result = finished[-1] if finished else train.optimize(dataset)
    frozen = STUDY / 'frozen-policy.json'
    if not frozen.exists():
        save(frozen, {'optimizer_result': result, 'dataset_sha256': file_sha(dataset),
             'source_protocol_sha256': file_sha(STUDY / 'protocol.json'),
             'frozen_before_any_compat2_test_result': True,
             'no_test_or_final_feedback_to_optimizer': True})
    elif json.loads(frozen.read_text(encoding='utf-8'))['optimizer_result'] != result:
        raise RuntimeError('frozen policy differs')
    progress('policy_frozen', promoted=result['promoted'], summary=summaries())
    by_name = {c['name']: c for c in study.CONTINUATION_CAMPAIGNS}
    for name in study.TEST_ORDER:
        if boundary():
            progress('safe_boundary', next_action=name, summary=summaries())
            return
        prepare_one(by_name[name])
        c = ResearchCampaign.load(STUDY / name)
        summaries()
        if any(e['kind'] == 'final_finished' for e in c.events()):
            continue
        if c.ensure_ready() < c.manifest['rounds']:
            invoke(study.run_argv(name), 'run-' + name)
        c = ResearchCampaign.load(STUDY / name)
        if not any(e['kind'] == 'final_finished' for e in c.events()):
            invoke(study.final_argv(name), 'final-' + name)
        progress('test_final_complete', campaign=name, summary=summaries())
    progress('compat2_complete', promoted=result['promoted'], summary=summaries())


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'dry-run', 'execute', 'status'))
    parser.add_argument('--boundary-seconds', type=float, default=1800)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            bootstrap()
        else:
            with FileLock(str(STUDY / 'driver.lock'), timeout=0):
                if args.command == 'status':
                    print(json.dumps(summaries(), ensure_ascii=False))
                elif args.command == 'dry-run':
                    dry_run()
                else:
                    execute(args.boundary_seconds)
    except BaseException as exc:
        safe = {'status': 'STOP', 'study': 'rsi-study-v4-compat2',
                'error_type': type(exc).__name__, 'historical_unknown_actual_usage': None,
                'old_v3_v4_compat1_unchanged': True}
        if args.command == 'execute':
            save(STUDY / 'STOP.json', safe)
        save(ROOT / 'ds1-compat2-stop-or-prepare-error.json', safe)
        print(json.dumps(safe), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
