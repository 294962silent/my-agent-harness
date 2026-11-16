"""Executor adapter for compat7's five remaining test arms."""
from __future__ import annotations

import argparse
import json
import time
import traceback

from filelock import FileLock
from experiments.research_validation import rsi_compat7 as study
from experiments.research_validation import rsi_compat4_execute as base
from my_coding_agent.research_runtime import atomic_bytes

ROOT, STUDY, WORKTREE = study.ROOT, study.STUDY, study.WORKTREE
SOURCE_FILES = list(base.SOURCE_FILES) + [
    WORKTREE / 'experiments/research_validation' / name for name in
    ('rsi_compat5.py', 'rsi_compat5_execute.py', 'rsi_compat6.py',
     'rsi_compat6_execute.py', 'rsi_compat7.py', 'rsi_compat7_execute.py')]
CLI_COMMAND = list(base.CLI_COMMAND)

base.study = study
base.ROOT = ROOT
base.STUDY = STUDY
base.WORKTREE = WORKTREE
base.SOURCE_FILES = SOURCE_FILES
base.CLI_COMMAND = CLI_COMMAND


def save(path, value):
    base.save(path, value)


def progress(stage, **fields):
    normalized = stage.replace('compat4', 'compat7').replace('compat6', 'compat7')
    value = {'executor': 'DS-1 deepseek-v4.1-flash max', 'study': 'rsi-study-v4-compat7',
             'stage': normalized, 'updated_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
             **fields}
    save(ROOT / 'ds1-compat7-progress.json', value)
    save(STUDY / 'progress.json', value)
    print(json.dumps({'stage': normalized, **fields}, ensure_ascii=False), flush=True)


def write_results(result):
    lines = ['# RSI compat7 剩余测试臂', '',
        'QSAR fixed/random 已完成；QSAR frozen 因 provider 未知用量停止，未重放。', '',
        '本批包括 QSAR greedy 与 AutomPG 四臂，均保留原坐标和预算。', '',
        '| campaign | arm | round RMSE | baseline RMSE | final RMSE | ratio | requests | tokens | evals | sec |',
        '|---|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for row in result['campaigns']:
        rnd = row['rounds'][0] if row['rounds'] else {}
        final = row['final'] or {}
        lines.append('| {} | {} | {} | {} | {} | {} | {} | {} | {} | {:.1f} |'.format(
            row['campaign'], row['arm'], rnd.get('selected_rmse', '—'), rnd.get('seed_rmse', '—'),
            final.get('rmse', '—'), final.get('rmse_over_mean_baseline', '—'),
            row['new_budget']['requests'], row['new_budget']['used_tokens'],
            row['new_budget']['evaluations'], row['new_budget']['wall_seconds']))
    atomic_bytes(STUDY / 'INTERVIEW_RESULTS.md', ('\n'.join(lines) + '\n').encode('utf-8'))


base.progress = progress
base.write_results = write_results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('prepare', 'dry-run', 'execute', 'status'))
    parser.add_argument('--boundary-seconds', type=float, default=21600)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            base.bootstrap()
        else:
            with FileLock(str(STUDY / 'driver.lock'), timeout=0):
                if args.command == 'status':
                    print(json.dumps(base.summaries(), ensure_ascii=False))
                elif args.command == 'dry-run':
                    base.dry_run()
                else:
                    base.execute(args.boundary_seconds)
    except BaseException as exc:
        if args.command in ('prepare', 'dry-run'):
            atomic_bytes(STUDY / f'{args.command}-private-traceback.txt',
                         traceback.format_exc().encode('utf-8'))
        safe = {'status': 'STOP', 'study': 'rsi-study-v4-compat7', 'error_type': type(exc).__name__,
                'historical_unknown_actual_usage': None, 'old_rsi_studies_unchanged': True,
                'provider_unknown_may_have_occurred': type(exc).__name__ in {
                    'RsiBudgetExceeded', 'ObservationRequiredError'}}
        if args.command == 'execute':
            save(STUDY / 'STOP.json', safe)
        save(ROOT / 'ds1-compat7-stop-or-prepare-error.json', safe)
        print(json.dumps(safe), flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
