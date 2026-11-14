"""CPU research CLI: python -m my_coding_agent.research_cli --help.

Credentials are resolved only by run/resume in the host process. Candidate
tools never receive a Config, environment secrets or host command capability.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import inspect
from pathlib import Path

from .research_runtime import ResearchCampaign, ResearchIntegrityError, credential_environment


def construct_llm(campaign, *, credential_env='RESEARCH_API_KEY', auth_provider=None, auth_profile=None):
    """Production AuthManager and Config; no dependency on experiment scripts."""
    from my_agent_llm import Config, LLM
    from my_agent_llm.auth.manager import AuthManager
    from my_agent_llm.auth.schema import ApiKeyCredential
    from .paths import AgentPaths
    model = dict(campaign.manifest['model'])
    if not model.get('model'):
        raise ResearchIntegrityError('campaign requires an explicit model before dispatch')
    key = os.environ.get(credential_env)
    if not key:
        credential = AuthManager(auth_path=AgentPaths().auth_path).get_credential(
            auth_provider or model.get('provider', 'openai'), profile=auth_profile)
        if isinstance(credential, ApiKeyCredential):
            if (credential.base_url or '').rstrip('/') != (model.get('base_url') or '').rstrip('/'):
                raise ResearchIntegrityError('credential endpoint differs from frozen provider endpoint')
            key = credential.resolve_key()
    if not key:
        raise ResearchIntegrityError('configured host provider credential unavailable')
    model['max_retries'] = 0
    return LLM(Config(**model, api_key=key))


def scrub_process_credentials(*extra_names):
    """CLI-only process mutation, after constructing authenticated host clients."""
    names = set(credential_environment()) | set(extra_names) | {'LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY'}
    for name in names:
        os.environ.pop(name, None)


async def close_owned_llm(raw):
    """CLI owns both clients it constructed; the LLM facade has no close method."""
    provider = getattr(raw, '_provider', None)
    clients, errors = [], []
    for name in ('async_client', 'client'):
        client = getattr(provider, name, None)
        if client is None or any(client is old for old in clients):
            continue
        clients.append(client)
        close = getattr(client, 'aclose', None) or getattr(client, 'close', None)
        if close:
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except BaseException as exc:
                errors.append(type(exc).__name__)
    return not errors


async def _execute(args, campaign):
    if args.command == 'final':
        scrub_process_credentials()
        return await campaign.final()
    from .research_integrations import ResearchObservation
    from .settings import load_settings
    raw = construct_llm(campaign, credential_env=args.credential_env,
                        auth_provider=args.auth_provider, auth_profile=args.auth_profile)
    observer = None
    try:
        observer = ResearchObservation(campaign.root / 'calls.jsonl', cloud=args.langfuse)
        observer.attach(raw)
        settings = load_settings().observation
        scrub_process_credentials(args.credential_env, settings.public_key_env, settings.secret_key_env)
        campaign.append('observation_started', {'local_metadata': True,
            'cloud_requested': args.langfuse, 'cloud_upload': observer.cloud_status})
        return await campaign.run(raw, pause_after_round=args.pause_after_round)
    finally:
        try:
            if observer is not None:
                campaign.append('observation_closed', observer.close())
        finally:
            closed = await close_owned_llm(raw)
            campaign.append('provider_clients_closed', {'closed': closed})


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest='command', required=True)
    prepare = commands.add_parser('prepare', help='freeze a new dataset split and campaign')
    prepare.add_argument('root', type=Path)
    prepare.add_argument('--csv', type=Path, required=True)
    prepare.add_argument('--target', required=True)
    prepare.add_argument('--id-column')
    prepare.add_argument('--task-root', type=Path)
    prepare.add_argument('--image', required=True, help='locally available immutable digest/image ID; never pulled')
    prepare.add_argument('--rounds', type=int, default=2)
    prepare.add_argument('--split', choices=('train', 'val', 'test'), default='train', help='strategy split, separate from row dev/final split')
    prepare.add_argument('--arm', default='C')
    prepare.add_argument('--seed', type=int, default=23)
    prepare.add_argument('--dev-fraction', type=float, default=.2)
    prepare.add_argument('--final-fraction', type=float, default=.2)
    prepare.add_argument('--provider', default='openai')
    prepare.add_argument('--model', required=True)
    prepare.add_argument('--base-url')
    prepare.add_argument('--temperature', type=float, default=.2)
    prepare.add_argument('--provider-timeout', type=int, default=90)
    prepare.add_argument('--max-requests', type=int, default=40)
    prepare.add_argument('--max-tokens', type=int, default=200000)
    prepare.add_argument('--max-seconds', type=float, default=600)
    prepare.add_argument('--max-evaluations', type=int, default=20)
    prepare.add_argument('--max-output-tokens', type=int, default=2048)
    prepare.add_argument('--width', type=int, default=2)
    prepare.add_argument('--refine', type=int, default=0)
    prepare.add_argument('--policy-from', type=Path, help='freeze promoted optimizer policy, else explicitly retain fixed reference')
    prepare.add_argument('--max-attempts', type=int, default=2)
    prepare.add_argument('--attempt-max-turns', type=int, default=4)
    prepare.add_argument('--context-budget', type=int, default=32000)
    prepare.add_argument('--schedule', choices=('rsi', 'fixed', 'random'), default='rsi')
    prepare.add_argument('--memory-source', type=Path)
    prepare.add_argument('--memory-mode', choices=('none', 'lexical', 'mem0'), default='lexical')
    prepare.add_argument('--mem0-model-dir', type=Path, help='preverified local_cpu_zh weights; never downloaded by run')
    prepare.add_argument('--docker-command-json', help='optional JSON argv prefix; otherwise native Docker/WSL detection')
    prepare.add_argument('--evaluation-timeout', type=float, default=60)
    for name in ('run', 'resume'):
        command = commands.add_parser(name, help='execute next unstarted round; interrupted rounds cannot replay')
        command.add_argument('root', type=Path)
        command.add_argument('--pause-after-round', type=int, help='absolute completed round count to stop at')
        command.add_argument('--credential-env', default='RESEARCH_API_KEY')
        command.add_argument('--auth-provider')
        command.add_argument('--auth-profile')
        command.add_argument('--langfuse', action='store_true', help='optional configured cloud export; delivery remains unverified')
    for name in ('status', 'final', 'optimize'):
        command = commands.add_parser(name)
        command.add_argument('root', type=Path)
        if name == 'optimize':
            command.add_argument('--dataset', type=Path)
            command.add_argument('--source-campaign', action='append', type=Path,
                                 help='export complete train/val dev traces from these campaigns first')
            command.add_argument('--dry-run', action='store_true')
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == 'prepare':
            from .research_task import ResearchTask
            from my_agent_llm import Config
            task_root = args.task_root or args.root.with_name(args.root.name + '-task')
            model = Config(provider=args.provider, model=args.model, base_url=args.base_url,
                max_tokens=args.max_output_tokens, max_retries=0, temperature=args.temperature,
                timeout=args.provider_timeout).model_dump(exclude={'api_key'})
            options = {'timeout': args.evaluation_timeout}
            if args.docker_command_json:
                options['docker_command'] = json.loads(args.docker_command_json)
            task = ResearchTask.prepare(args.csv, task_root, target=args.target, id_column=args.id_column,
                seed=args.seed, dev_fraction=args.dev_fraction, final_fraction=args.final_fraction)
            campaign = ResearchCampaign.prepare(task, args.root, image=args.image, rounds=args.rounds,
                arm=args.arm, split=args.split, model=model,
                parameters={'max_width': args.width, 'max_refine': args.refine},
                limits=dict(max_requests=args.max_requests, max_tokens=args.max_tokens,
                    max_seconds=args.max_seconds, max_evaluations=args.max_evaluations,
                    max_output_tokens=args.max_output_tokens),
                max_attempts=args.max_attempts, attempt_max_turns=args.attempt_max_turns,
                context_budget=args.context_budget, schedule=args.schedule,
                evaluator_options=options, memory_source=args.memory_source, memory_mode=args.memory_mode,
                policy_from=args.policy_from,
                mem0_config=({'profile': 'local_cpu_zh', 'model_dir': str(args.mem0_model_dir.resolve()), 'threads': 2}
                             if args.mem0_model_dir is not None else None))
            value = campaign.status()
        else:
            campaign = ResearchCampaign.load(args.root)
            if args.command == 'status':
                value = campaign.status()
            elif args.command == 'optimize':
                dataset = args.dataset
                if args.source_campaign:
                    if dataset is not None:
                        raise ValueError('choose explicit dataset or campaign export')
                    dataset = ResearchCampaign.export_dataset(args.source_campaign, campaign.root / 'optimizer-dataset')
                value = campaign.optimize(dataset, dry_run=args.dry_run)
            else:
                value = asyncio.run(_execute(args, campaign))
        print(json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False))
        return 0
    except (Exception, KeyboardInterrupt) as exc:
        # Provider exceptions may contain authenticated URLs/headers: never print them.
        print(json.dumps({'status': 'blocked', 'error_type': type(exc).__name__}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
