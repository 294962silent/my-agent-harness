"""Execute the explicit compat4 eight-arm test-only minimal subset."""
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
import traceback

from filelock import FileLock
from experiments.research_validation import rsi_compat4 as study
from experiments.research_validation import rsi_observation
from my_coding_agent.research_runtime import ResearchCampaign, atomic_bytes, canonical
from my_coding_agent.rsi_branch import file_sha

ROOT, STUDY, WORKTREE = study.ROOT, study.STUDY, study.WORKTREE
MODULES = ('research_runtime.py', 'research_cli.py', 'research_task.py', 'research_evaluator.py',
           'research_integrations.py', 'research_continuation.py', 'rsi_runtime.py', 'rsi_branch.py',
           'dream_rsi_contract.py', 'dream_rsi_optimizer.py', 'dream_rsi_registry.py',
           'dream_rsi_replay.py', '_dream_rsi_worker.py')
SOURCE_FILES = [WORKTREE / 'src/my_coding_agent' / name for name in MODULES]
SOURCE_FILES += [WORKTREE / 'experiments/research_validation' / name for name in
                 ('rsi_study.py', 'rsi_continuation.py', 'rsi_observation.py', 'rsi_v4_execute.py',
                  'rsi_compat.py', 'rsi_compat_execute.py', 'rsi_compat2.py',
                  'rsi_compat2_execute.py', 'rsi_compat3.py', 'rsi_compat3_execute.py',
                  'rsi_compat4.py', 'rsi_compat4_execute.py')]
SOURCE_FILES += [WORKTREE / 'experiments/benchmark_live/observation.py']
SOURCE_FILES += [WORKTREE / 'src/my_agent_llm' / name for name in
                 ('observation.py', 'observation_runtime.py', 'observation_export.py',
                  'langfuse_export.py', 'client.py', 'providers/openai.py')]
CLI_COMMAND = ['D:/soft/node/node.exe',
    'C:/Users/MLL/AppData/Local/npm-cache/_npx/6a6af7828f1bc5cc/node_modules/langfuse-cli/bin/langfuse.mjs']


def save(path, value):
    atomic_bytes(path, canonical(value))


def source_check():
    protocol = json.loads((STUDY / 'protocol.json').read_text(encoding='utf-8'))
    for row in protocol['source_pins']:
        if file_sha(Path(row['path'])) != row['sha256']:
            raise RuntimeError('compat4 frozen source drift: ' + Path(row['path']).name)
    study.verify_parent()
    if protocol.get('compatibility_parent_evidence') != study.parent_evidence():
        raise RuntimeError('compat3 evidence changed after compat4 freeze')
    if protocol['budget_plan'] != study.budget_plan():
        raise RuntimeError('compat4 budget plan changed')
    if protocol['optimizer_decision'] != study.optimizer_decision():
        raise RuntimeError('compat4 optimizer fallback decision changed')
    return protocol


def progress(stage, **fields):
    value = {'executor': 'DS-1 deepseek-v4.1-flash max', 'study': 'rsi-study-v4-compat4',
             'stage': stage, 'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
             **fields}
    save(ROOT / 'ds1-compat4-progress.json', value)
    save(STUDY / 'progress.json', value)
    print(json.dumps({'stage': stage, **fields}, ensure_ascii=False), flush=True)


def summaries():
    values, budgets = [], []
    for spec in study.TEST_CAMPAIGNS:
        path = STUDY / spec['name']
        if not (path / 'campaign.json').exists():
            continue
        c = ResearchCampaign.load(path)
        events = c.events()
        state = c.status()
        budget = state['budget']
        persisted = [row['payload']['budget_state'] for row in events if 'budget_state' in row['payload']]
        budget['wall_seconds'] = persisted[-1]['active_seconds'] if persisted else 0
        budgets.append(budget)
        rounds = []
        for row in c.committed_rounds():
            payload = row['payload']
            report = json.loads((c.root / payload['report']).read_text(encoding='utf-8'))
            baseline = report['baseline']
            selected = baseline if payload['selection_source'] == 'baseline' else next(
                attempt['visible'] for attempt in report['attempts']
                if attempt['id'] == payload['raw_agent_candidate_id'])
            rounds.append({'round': payload['round'], 'seed_rmse': baseline.get('rmse'),
                'selected_rmse': selected.get('rmse'), 'utility': payload['score'],
                'selection_source': payload['selection_source'],
                'known_invalid_attempts': report['invalid_attempts'],
                'trace_complete': payload['trace'] is not None,
                'fallback_event': any(event['kind'] == 'policy_fallback_applied' for event in events)})
        final = state['final']
        if final is not None:
            with c.task.train_csv.open(encoding='utf-8', newline='') as stream:
                mean = statistics.mean(float(row[c.task.manifest['target']]) for row in csv.DictReader(stream))
            with (c.task.root / 'private/final_labels.csv').open(encoding='utf-8', newline='') as stream:
                labels = [float(row['target']) for row in csv.DictReader(stream)]
            baseline_final = math.hypot(*[(mean-value) / math.sqrt(len(labels)) for value in labels])
            final = {**final, 'mean_baseline_rmse': baseline_final,
                     'rmse_over_mean_baseline': final['rmse'] / baseline_final if baseline_final else None}
        values.append({'campaign': spec['name'], 'campaign_id': c.manifest['campaign_id'],
            'arm': c.manifest['arm'], 'rounds': rounds, 'new_budget': budget, 'final': final,
            'remote_readback_samples': [row['payload'] for row in events
                                        if row['kind'] == 'observation_readback_verified'],
            'policy_origin': c.manifest['policy_origin'],
            'fallback_policy_origin': spec.get('fallback_policy_origin')})
        kinds = [row['kind'] for row in events]
        if (kinds.count('round_started') != kinds.count('round_committed') or
                ('final_started' in kinds) != ('final_finished' in kinds)):
            raise RuntimeError('unfinished compat4 test campaign: ' + spec['name'])
    usage = study.assert_joint_budget(budgets)
    result = {'campaigns': values, 'usage': usage, 'inherited_cost': study.HISTORICAL_USAGE,
              'optimizer_decision': study.optimizer_decision()}
    save(STUDY / 'joint-usage.json', usage)
    save(STUDY / 'last-summary.json', result)
    return result


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


def _command_kind(argv, name):
    try:
        index = argv.index('my_coding_agent.research_cli')
    except ValueError as exc:
        raise RuntimeError('research CLI module entry missing') from exc
    if argv[index + 1:index + 2] != [name]:
        raise RuntimeError('research CLI command mismatch')


def verify_prepared(c, spec):
    manifest = c.manifest
    if (any(manifest['limits'].get(key) != value for key, value in spec['limits'].items()) or
            manifest['delivery_contract'] != 'artifact_budget_v1' or
            manifest['attempt_max_turns'] != 4 or manifest['optimization']['budgets'] != [1, 2, 3] or
            manifest.get('observation_contract') != 'langfuse_campaign_v1' or
            manifest['observation_protocol']['sha256'] != file_sha(STUDY / 'protocol.json') or
            manifest.get('planner_seed') != (20261114 if spec['schedule'] == 'random' else None) or
            manifest.get('policy_origin') is not None):
        raise RuntimeError('compat4 test campaign differs from frozen subset')


def prepare_one(spec):
    path = STUDY / spec['name']
    if not (path / 'campaign.json').exists():
        if path.exists():
            raise RuntimeError('partial preparation preserved: ' + spec['name'])
        invoke(study.prepare_argv(spec), 'prepare-' + spec['name'])
    c = ResearchCampaign.load(path)
    verify_prepared(c, spec)
    if spec.get('fallback_policy_origin') and not any(
            event['kind'] == 'policy_fallback_applied' for event in c.events()):
        c.append('policy_fallback_applied', {'origin': spec['fallback_policy_origin'],
            'optimizer_decision_sha256': hashlib.sha256(canonical(study.optimizer_decision())).hexdigest(),
            'same_parameters': {'max_width': 2, 'max_refine': 1},
            'learning_effect': 'unidentifiable'})
    return c


def bootstrap():
    if not STUDY.exists():
        protocol = study.build_protocol(SOURCE_FILES, cli_command=CLI_COMMAND,
                                        base_url='https://cloud.langfuse.com')
        STUDY.mkdir()
        save(STUDY / 'protocol.json', protocol)
        save(STUDY / 'source-pins.json', protocol['source_pins'])
        save(STUDY / 'carry-forward.json', {'historical_usage': study.HISTORICAL_USAGE,
            'compat3_parent_evidence': protocol['compatibility_parent_evidence'],
            'optimizer_decision': protocol['optimizer_decision']})
        for index, pin in enumerate(protocol['source_pins']):
            atomic_bytes(STUDY / 'source-frozen' / f'{index:02d}-{Path(pin["path"]).name}',
                         Path(pin['path']).read_bytes())
    protocol = source_check()
    save(STUDY / 'optimizer-decision.json', protocol['optimizer_decision'])
    for spec in study.TEST_CAMPAIGNS:
        prepare_one(spec)
    progress('compat4_frozen_offline_prepared', protocol_sha256=file_sha(STUDY / 'protocol.json'),
             source_manifest_sha256=file_sha(STUDY / 'source-pins.json'), new_paid_requests=0,
             new_evaluations=0, summary=summaries())


def dry_run():
    protocol = source_check()
    if not shutil.which('uv'):
        raise RuntimeError('uv executable unavailable')
    observation = protocol['observation']
    if observation['readback_timeout_seconds'] < 180 or distribution_version('langfuse') != observation['sdk_version']:
        raise RuntimeError('compat4 observation stack changed')
    if any(file_sha(Path(path)) != digest for path, digest in observation['cli_pins'].items()):
        raise RuntimeError('frozen Langfuse CLI changed')
    env = rsi_observation.load_observation_environment()
    base = env.get('LANGFUSE_BASE_URL') or env.get('LANGFUSE_HOST')
    if (not env.get('LANGFUSE_PUBLIC_KEY') or not env.get('LANGFUSE_SECRET_KEY') or
            (base or '').rstrip('/') != observation['base_url'].rstrip('/')):
        raise RuntimeError('required Langfuse configuration unavailable/mismatched')
    env.clear()
    plan = []
    for spec in study.TEST_CAMPAIGNS:
        prepare_one(spec)
        run, final = study.run_argv(spec['name']), study.final_argv(spec['name'])
        _command_kind(run, 'run')
        _command_kind(final, 'final')
        plan.append({'campaign': spec['name'], 'arm': spec['arm'], 'run_argv': run,
                     'final_argv': final, 'fallback': spec.get('fallback_policy_origin')})
    evidence = {'version': 1, 'status': 'PASS', 'new_paid_model_requests': 0,
        'new_evaluations': 0, 'protocol_sha256': file_sha(STUDY / 'protocol.json'),
        'source_manifest_sha256': file_sha(STUDY / 'source-pins.json'),
        'campaigns': plan, 'langfuse': {'required': True, 'credentials_present': True,
            'readback_timeout_seconds': 180, 'provider_retries': 0},
        'optimizer_decision': protocol['optimizer_decision'], 'budget_plan': protocol['budget_plan']}
    save(STUDY / 'driver-smoke.json', evidence)
    save(ROOT / 'ds1-compat4-driver-smoke.json', evidence)
    progress('offline_driver_dry_run_passed', new_paid_requests=0, new_evaluations=0,
             test_campaigns=len(plan))
    return evidence


def write_results(result):
    lines = ['# RSI compat4 最小可比子集', '',
        'Wine holdout 在 provider 用量未知后按 fail-closed 停止；本批未重放旧请求，也未运行 optimizer。', '',
        '`trained-frozen` 因无可利用的 holdout 晋升，明确使用 fixed-reference 参数；学习增益不可识别。', '',
        '| campaign | arm | round RMSE | baseline RMSE | selection | final RMSE | baseline ratio | requests | tokens | evals | sec |',
        '|---|---|---:|---:|---|---:|---:|---:|---:|---:|---:|']
    for row in result['campaigns']:
        rnd = row['rounds'][0] if row['rounds'] else {}
        final = row['final'] or {}
        lines.append('| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {:.1f} |'.format(
            row['campaign'], row['arm'], rnd.get('selected_rmse', '—'), rnd.get('seed_rmse', '—'),
            rnd.get('selection_source', '—'), final.get('rmse', '—'),
            final.get('rmse_over_mean_baseline', '—'), row['new_budget']['requests'],
            row['new_budget']['used_tokens'], row['new_budget']['evaluations'],
            row['new_budget']['wall_seconds']))
    usage = result['usage']
    lines += ['', '## 用量', '',
        f"- 历史（含 compat3 的 2 个未知请求）：{usage['historical']['requests']} requests / "
        f"{usage['historical']['accounting_units']} accounting units / {usage['historical']['evaluations']} evals / "
        f"{usage['historical']['active_seconds']:.1f}s。",
        f"- compat4：{usage['new']['requests']} requests / {usage['new']['accounting_units']} units / "
        f"{usage['new']['evaluations']} evals / {usage['new']['active_seconds']:.1f}s。",
        '- 这是单 seed 的两数据集四臂测试；Wine 训练/验证缺失，不能外推到普遍 RSI 收益。']
    atomic_bytes(STUDY / 'INTERVIEW_RESULTS.md', ('\n'.join(lines) + '\n').encode('utf-8'))


def execute(boundary_seconds):
    if (STUDY / 'STOP.json').exists():
        raise RuntimeError('compat4 STOP is permanent; create a new explicit continuation')
    source_check()
    start = time.monotonic()
    for spec in study.TEST_CAMPAIGNS:
        if time.monotonic() - start >= boundary_seconds:
            progress('safe_boundary', next_action=spec['name'], summary=summaries())
            return
        c = prepare_one(spec)
        if any(event['kind'] == 'final_finished' for event in c.events()):
            continue
        if c.ensure_ready() < c.manifest['rounds']:
            invoke(study.run_argv(spec['name']), 'run-' + spec['name'])
        c = ResearchCampaign.load(STUDY / spec['name'])
        if not any(event['kind'] == 'final_finished' for event in c.events()):
            invoke(study.final_argv(spec['name']), 'final-' + spec['name'])
        progress('test_final_complete', campaign=spec['name'], summary=summaries())
    result = summaries()
    write_results(result)
    progress('compat4_test_subset_complete', summary=result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'dry-run', 'execute', 'status'))
    parser.add_argument('--boundary-seconds', type=float, default=21600)
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
        if args.command in ('prepare', 'dry-run'):
            atomic_bytes(STUDY / f'{args.command}-private-traceback.txt',
                         traceback.format_exc().encode('utf-8'))
        safe = {'status': 'STOP', 'study': 'rsi-study-v4-compat4', 'error_type': type(exc).__name__,
                'historical_unknown_actual_usage': None,
                'old_rsi_studies_unchanged': True,
                'provider_unknown_may_have_occurred': type(exc).__name__ in {
                    'RsiBudgetExceeded', 'ObservationRequiredError'}}
        if args.command == 'execute':
            save(STUDY / 'STOP.json', safe)
        save(ROOT / 'ds1-compat4-stop-or-prepare-error.json', safe)
        print(json.dumps(safe), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
