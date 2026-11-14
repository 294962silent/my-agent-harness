"""One fresh dev-only budget calibration, using production research entrypoints.

Prepare never dispatches a provider. Run is an explicit operator action; there is
no final, resume, retry, parameter search, memory loading or weight download here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from my_coding_agent import research_cli
from my_coding_agent.research_runtime import ResearchCampaign
from my_coding_agent.research_task import ResearchTask

STUDY = 'calibration-v2'
LIMITS = dict(max_requests=20, max_tokens=400000, max_seconds=1200,
              max_evaluations=4, max_output_tokens=16384)


def prepare(csv, root, *, image, model, provider='openai', base_url=None,
            docker_command=None, seed=23):
    """Freeze one new calibration; an existing directory is never reused."""
    from my_agent_llm import Config
    root = Path(root).resolve()
    task_root = root.with_name(root.name + '-task')
    if root.exists() or task_root.exists():
        raise ValueError('calibration and task directories must both be new')
    task = ResearchTask.prepare(csv, task_root, target='quality', seed=seed,
                                dev_fraction=.2, final_fraction=.2)
    model_config = Config(provider=provider, model=model, base_url=base_url,
                          max_tokens=16384, max_retries=0, temperature=.2, timeout=180)
    options = {'timeout': 60}
    if docker_command is not None:
        options['docker_command'] = list(docker_command)
    return ResearchCampaign.prepare(task, root, image=image, rounds=1, arm=STUDY,
        split='train', limits=dict(LIMITS), model=model_config.model_dump(exclude={'api_key'}),
        parameters={'max_width': 1, 'max_refine': 1}, max_attempts=2,
        attempt_max_turns=8, context_budget=64000, schedule='rsi',
        evaluator_options=options, memory_mode='none')


def verify_calibration(campaign):
    """Fail before credentials or dispatch if this is not the approved study."""
    plan = campaign.manifest
    expected = {'rounds': 1, 'arm': STUDY, 'split': 'train',
        'max_attempts': 2, 'attempt_max_turns': 8, 'memory_mode': 'none', 'schedule': 'rsi',
        'context_budget': 64000}
    if any(plan.get(k) != v for k, v in expected.items()):
        raise ValueError('campaign differs from approved calibration')
    if (plan.get('limits') != LIMITS or
            plan['policy']['parameters'] != {'max_width': 1, 'max_refine': 1} or
            plan['model'].get('max_tokens') != 16384 or plan['model'].get('max_retries') != 0 or
            plan['model'].get('timeout') != 180):
        raise ValueError('calibration budget/model/policy differs')
    return campaign


def report(root):
    """Only summarize dev metadata; never run or read the final scorer/labels."""
    campaign = verify_calibration(ResearchCampaign.load(root))
    reports = []
    for path in sorted(campaign.root.glob('rounds/*/result.json')):
        value = json.loads(path.read_text(encoding='utf-8'))
        original = hashlib.sha256((path.parent / 'baseline/train.py').read_bytes()).hexdigest()
        attempts = []
        seen_messages = set()
        for row in value['attempts']:
            checkpoint = Path(row['checkpoint']) / 'train.py' if row.get('checkpoint') else None
            code_sha = hashlib.sha256(checkpoint.read_bytes()).hexdigest() if checkpoint is not None else None
            changed = code_sha != original if code_sha is not None else None
            writes = {a.get('tool_call_id') for a in row.get('actions', [])
                      if a.get('tool_name') in ('write', 'edit')}
            lengths = None
            if checkpoint is not None:
                snapshot = checkpoint.parent.parent / 'session-snapshot.jsonl'
                if snapshot.is_file():
                    lengths = 0
                    for line in snapshot.read_text(encoding='utf-8').splitlines():
                        message = json.loads(line)
                        if message.get('id') in seen_messages:
                            continue
                        seen_messages.add(message.get('id'))
                        payload = message.get('message', {})
                        lengths += payload.get('role') == 'assistant' and payload.get('metadata', {}).get('stop_reason') == 'length'
            attempts.append({'id': row['id'], 'terminal': row['agent_terminal'],
                'valid': row['valid'], 'score': (row.get('visible') or {}).get('score'),
                'predictions_valid': (row.get('visible') or {}).get('valid'),
                'code_sha256': code_sha, 'code_changed': changed,
                'write_edit_calls': len(writes), 'length_count': lengths})
        trace = path.parent / 'trace.json'
        trace_verified = (trace.is_file() and
            hashlib.sha256(trace.read_bytes()).hexdigest() == value['trace_sha256'])
        reports.append({'round': path.parent.name, 'status': value['status'],
            'baseline_score': (value.get('baseline') or {}).get('score'), 'baseline_code_sha256': original,
            'selected_score': value['selected_visible_score'], 'attempts': attempts,
            'complete_trace': trace_verified, 'budget': value['budget'],
            'calibrated': len(attempts) == 2 and trace_verified and
                all(a['valid'] and a['terminal'].get('stop_reason') == 'end_turn' for a in attempts) and
                any(a['code_changed'] for a in attempts)})
    return {'study': STUDY, 'scope': 'dev-only; red final previously seen',
            'campaign_status': campaign.status(), 'rounds': reports}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    prep = sub.add_parser('prepare')
    prep.add_argument('root', type=Path)
    prep.add_argument('--csv', type=Path, required=True)
    prep.add_argument('--image', required=True)
    prep.add_argument('--model', required=True)
    prep.add_argument('--provider', default='openai')
    prep.add_argument('--base-url')
    prep.add_argument('--docker-command-json')
    run = sub.add_parser('run')
    run.add_argument('root', type=Path)
    run.add_argument('--credential-env', default='RESEARCH_API_KEY')
    run.add_argument('--auth-provider')
    run.add_argument('--auth-profile')
    view = sub.add_parser('report')
    view.add_argument('root', type=Path)
    args = parser.parse_args(argv)
    if args.command == 'prepare':
        campaign = prepare(args.csv, args.root, image=args.image, model=args.model,
            provider=args.provider, base_url=args.base_url,
            docker_command=json.loads(args.docker_command_json) if args.docker_command_json else None)
        print(json.dumps(campaign.status(), ensure_ascii=False))
    elif args.command == 'run':
        campaign = verify_calibration(ResearchCampaign.load(args.root))
        if any(e['kind'] != 'prepared' for e in campaign.events()):
            raise ValueError('calibration already started; no automatic retry/resume')
        command = ['run', str(args.root), '--credential-env', args.credential_env]
        for key in ('auth_provider', 'auth_profile'):
            if getattr(args, key):
                command += ['--' + key.replace('_', '-'), getattr(args, key)]
        return research_cli.main(command)
    else:
        print(json.dumps(report(args.root), ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
