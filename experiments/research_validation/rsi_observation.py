"""RSI assembly of the shared raw observation owner and bounded readback gate.

No GuardedLLM is constructed here: SharedLLM/ResearchBudget owns model accounting.
Only opaque scope, terminal metadata and provider usage are exported/read back.
"""
from __future__ import annotations

import base64
import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

from experiments.benchmark_live.observation import ExperimentLLMOwner
from my_agent_llm.observation import CallEnd
from my_coding_agent.research_runtime import ResearchBudget, ResearchIntegrityError, atomic_bytes, canonical
from my_coding_agent.rsi_branch import file_sha


def load_frozen_protocol(campaign):
    pin = campaign.manifest['observation_protocol']
    path = Path(pin['file'])
    if file_sha(path) != pin['sha256']:
        raise ResearchIntegrityError('observation protocol changed')
    protocol = json.loads(path.read_text(encoding='utf-8'))
    version = protocol.get('version')
    supported = version in ('rsi-study-v4-continuation',)
    supported = supported or (isinstance(version, str) and version.startswith('rsi-study-v4-compat') and
                              version.removeprefix('rsi-study-v4-compat').isdigit())
    if not supported:
        raise ResearchIntegrityError('unsupported observation protocol')
    for row in protocol['source_pins']:
        if file_sha(Path(row['path'])) != row['sha256']:
            raise ResearchIntegrityError('frozen source pin changed')
    return protocol


def load_observation_environment():
    """Explicit host-only HKCU fallback; retain no credential in files/results."""
    names = ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL', 'LANGFUSE_HOST')
    if os.name == 'nt':
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, 'Environment') as key:
            for name in names:
                if not os.environ.get(name):
                    try:
                        value, _ = winreg.QueryValueEx(key, name)
                        if isinstance(value, str) and value:
                            os.environ[name] = value
                    except FileNotFoundError:
                        pass
    return {name: os.environ[name] for name in names if os.environ.get(name)}


def parent_metadata(value):
    """The fixed CLI expands JSON metadata into objects; legacy rows use text."""
    try:
        if type(value) is str:
            def reject_constant(_value):
                raise ValueError('nonfinite JSON')
            value = json.loads(value, parse_constant=reject_constant)
        if type(value) is not dict:
            raise ValueError('parent metadata must be an object')
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, TypeError) as exc:
        raise ResearchIntegrityError('invalid observation parent_id metadata') from exc


def match_remote(observations, calls, *, campaign, root_id):
    """Return only complete matching metadata; never expose input/output bodies."""
    found = {}
    expected_stage = {'train': 'train', 'val': 'holdout', 'test': 'test'}[campaign.manifest['split']]
    trace_id = hashlib.sha256(root_id.encode()).hexdigest()[:32]
    for obs in observations:
        meta = obs.get('metadata') or {}
        call_id = meta.get('call_id')
        if call_id not in calls:
            continue
        call = calls[call_id]
        parent = json.loads(call.parent_id)
        remote_parent = parent_metadata(meta.get('parent_id'))
        expected = {'campaign': campaign.manifest['campaign_id'], 'arm': campaign.manifest['arm'],
                    'stage': expected_stage, 'task_id': campaign.task.manifest_sha256,
                    'root_id': root_id, 'call_id': call_id, 'status': call.status}
        if (any(meta.get(k) != v for k, v in expected.items()) or
                obs.get('sessionId') != campaign.manifest['campaign_id'] or
                obs.get('traceId') != trace_id or not obs.get('endTime') or remote_parent != parent):
            continue
        usage = obs.get('usageDetails') or {}
        local = dict(call.usage or {})
        if usage.get('total') != local.get('total_tokens'):
            continue
        observation_id = obs.get('id')
        if not observation_id or any(row['observation_id'] == observation_id for row in found.values()):
            continue
        found[call_id] = {'call_id': call_id, 'observation_id': observation_id, 'trace_id': trace_id,
            'session_id': obs['sessionId'], 'arm': expected['arm'], 'stage': expected_stage,
            'task_id': expected['task_id'], 'scope': parent, 'status': call.status,
            'usage_details': {k: v for k, v in usage.items() if k in ('input', 'output', 'total')},
            'model': obs.get('model'), 'end_time': obs['endTime']}
    return found


class RemoteReadbackGate:
    """Read back the first two ordinary calls before reserving a third request."""
    def __init__(self, campaign, owner, protocol, credential_env, *, query=None, wait=time.sleep):
        self.campaign, self.owner, self.protocol = campaign, owner, protocol
        self.config = protocol['observation']
        self.credential_env = dict(credential_env)
        self.offset = len(ResearchBudget(campaign).records)
        self.calls, self.verified = {}, False
        self.sampled_attempts, self.verified_ids = set(), set()
        self.query = query or self._query
        self.wait = wait
        self.subscription = owner.raw.add_observer(self.capture)

    def capture(self, record):
        if isinstance(record, CallEnd):
            parent = json.loads(record.parent_id or '{}')
            key = tuple(parent.get(k) for k in ('round', 'branch', 'attempt'))
            if len(self.calls) < 2 or key not in self.sampled_attempts:
                self.calls[record.call_id] = record
                self.sampled_attempts.add(key)

    def _query(self, root_id, remaining):
        trace_id = hashlib.sha256(root_id.encode()).hexdigest()[:32]
        query = urllib.parse.urlencode({'traceId': trace_id, 'fields': 'core,basic,model,usage,metadata',
                                        'expandMetadata': 'parent_id', 'limit': 100})
        url = self.config['base_url'].rstrip('/') + '/api/public/v2/observations?' + query
        auth = (self.credential_env['LANGFUSE_PUBLIC_KEY'] + ':' + self.credential_env['LANGFUSE_SECRET_KEY']).encode()
        request = urllib.request.Request(url, headers={'Authorization': 'Basic ' + base64.b64encode(auth).decode(),
                                                      'Accept': 'application/json'})
        timeout = max(.1, min(self.config.get('readback_request_timeout_seconds', 20), remaining))
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                if response.status != 200:
                    raise ResearchIntegrityError('Langfuse metadata HTTP readback unavailable')
                body = json.load(response)
        except urllib.error.HTTPError as exc:
            raise ResearchIntegrityError(f'Langfuse metadata HTTP readback failed: HTTP {exc.code}') from None
        except (urllib.error.URLError, TimeoutError) as exc:
            cause = exc.reason if isinstance(exc, urllib.error.URLError) else exc
            if isinstance(cause, TimeoutError):
                raise subprocess.TimeoutExpired('Langfuse readonly HTTP metadata', timeout) from None
            raise ResearchIntegrityError('Langfuse metadata HTTP transport failed: ' + type(cause).__name__) from None
        except (ValueError, UnicodeError, OSError):
            raise ResearchIntegrityError('Langfuse metadata HTTP response invalid') from None
        if type(body) is not dict or type(body.get('data')) is not list or any(type(r) is not dict for r in body['data']):
            raise ResearchIntegrityError('Langfuse metadata HTTP response shape invalid')
        return body['data']

    def __call__(self, budget, *, boundary=False):
        load_frozen_protocol(self.campaign)
        count = len(budget.records) - self.offset
        # ResearchBudget has settled the preceding call before this hook.
        self.owner.flush_and_check(timeout=10)
        if (set(self.calls) <= self.verified_ids or (count < 2 and not boundary) or count == 0):
            return
        if budget.blocked or not budget.snapshot()['usage_complete'] or len(self.calls) < min(count, 2):
            raise ResearchIntegrityError('cannot read back incomplete/unknown requests')
        if any(c.status != 'ok' or c.root_id != budget.root_id for c in self.calls.values()):
            raise ResearchIntegrityError('observation terminal metadata is incomplete')
        if not self.owner.runtime.exporter.flush(timeout=10):
            raise ResearchIntegrityError('observation flush incomplete')
        if self.owner.runtime.export_errors or self.owner.runtime.dropped:
            raise ResearchIntegrityError('observation exporter lost metadata')
        deadline = time.monotonic() + self.config['readback_timeout_seconds']
        query_limit = self.config.get('max_readonly_queries_per_gate', 3)
        if type(query_limit) is not int or not 1 <= query_limit <= 16:
            raise ResearchIntegrityError('invalid bounded readback query policy')
        initial_backoff = self.config.get('readback_backoff_initial_seconds', 1)
        max_backoff = self.config.get('readback_backoff_max_seconds', 15)
        if (type(initial_backoff) not in (int, float) or initial_backoff <= 0 or
                type(max_backoff) not in (int, float) or max_backoff < initial_backoff):
            raise ResearchIntegrityError('invalid readback backoff policy')
        query_attempts = []
        last_error = None
        for index in range(query_limit):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                remote = self.query(budget.root_id, remaining)
            except (subprocess.TimeoutExpired, ResearchIntegrityError) as exc:
                last_error = exc
                found = {}
                query_attempts.append({'query': index + 1, 'outcome': 'error',
                                       'error_type': type(exc).__name__})
            else:
                found = match_remote(remote, self.calls, campaign=self.campaign, root_id=budget.root_id)
                query_attempts.append({'query': index + 1, 'outcome': 'matched' if len(found) == len(self.calls)
                                       else 'incomplete', 'matched': len(found), 'expected': len(self.calls)})
                if len(found) == len(self.calls):
                    evidence = {'version': 2, 'status': 'verified_remote_metadata',
                        'sample_request_start': self.offset + 1, 'sample_count': len(found),
                        'rows': list(found.values()), 'io_requested': False,
                        'readonly_queries': query_attempts,
                        'readback_timeout_seconds': self.config['readback_timeout_seconds'],
                        'max_readonly_queries_per_gate': query_limit,
                        'backoff': 'bounded exponential; observation API only',
                        'extra_model_requests': 0, 'historical_requests_uploaded': False}
                    path = self.campaign.root / f'observation-readback-{self.offset+1:04d}-{len(found):04d}.json'
                    atomic_bytes(path, canonical(evidence))
                    self.campaign.append('observation_readback_verified', {'file': path.name, 'sha256': file_sha(path),
                        'sample_count': len(found), 'sample_request_start': self.offset + 1,
                        'readonly_query_count': len(query_attempts), 'extra_model_requests': 0})
                    self.verified_ids.update(found)
                    self.verified = True
                    return
            if index + 1 < query_limit:
                delay = min(max_backoff, initial_backoff * (2 ** index),
                            max(0, deadline - time.monotonic()))
                if delay > 0:
                    query_attempts[-1]['backoff_seconds'] = delay
                    self.wait(delay)
        if isinstance(last_error, subprocess.TimeoutExpired):
            raise ResearchIntegrityError('observation readback deadline exceeded') from None
        raise ResearchIntegrityError('first ordinary calls not remotely verified; further dispatch blocked')

    def close(self):
        self.subscription.close()
        self.credential_env.clear()


async def run_observed_campaign(args, campaign):
    from my_coding_agent.research_cli import construct_llm, scrub_process_credentials, close_owned_llm
    from my_coding_agent.research_integrations import ResearchObservation
    protocol = load_frozen_protocol(campaign)
    config = protocol['observation']
    if version('langfuse') != config['sdk_version']:
        raise ResearchIntegrityError('frozen Langfuse SDK version changed')
    for path, sha in config['cli_pins'].items():
        if file_sha(Path(path)) != sha:
            raise ResearchIntegrityError('frozen Langfuse CLI changed')
    env = load_observation_environment()
    base_url = env.get('LANGFUSE_BASE_URL') or env.get('LANGFUSE_HOST')
    if (not env.get('LANGFUSE_PUBLIC_KEY') or not env.get('LANGFUSE_SECRET_KEY') or
            (base_url or '').rstrip('/') != config['base_url'].rstrip('/')):
        raise ResearchIntegrityError('required Langfuse configuration unavailable/mismatched')
    raw = construct_llm(campaign, credential_env=args.credential_env,
        auth_provider=args.auth_provider, auth_profile=args.auth_profile)
    owner = local = gate = None
    try:
        owner = ExperimentLLMOwner.from_environment(raw, campaign_id=campaign.manifest['campaign_id'],
            enabled=True, base_url=base_url, required=True)
        local = ResearchObservation(campaign.root / 'calls.jsonl', cloud=False)
        local.attach(raw)
        gate = RemoteReadbackGate(campaign, owner, protocol, env)
        env.clear()
        scrub_process_credentials(args.credential_env)
        campaign.append('observation_started', {'local_metadata': True, 'cloud_requested': True,
            'contract': 'langfuse_campaign_v1', 'sdk_version': config['sdk_version'],
            'scope': 'stable campaign/session; per-call parent_id round/branch/attempt',
            'cloud_upload': 'configured_pending_remote_readback'})
        result = await campaign.run(raw, pause_after_round=args.pause_after_round,
                                   observation_owner=owner, before_request=gate)
        await owner.aflush_and_check(timeout=10)
        gate(ResearchBudget(campaign), boundary=True)
        return result
    finally:
        env.clear()
        scrub_process_credentials(args.credential_env)
        if gate is not None:
            gate.close()
        try:
            if local is not None:
                local_result = local.close()
                campaign.append('observation_local_closed', local_result)
                if (not local_result.get('local_closed') or local_result.get('local_dropped') or
                        local_result.get('local_export_errors')):
                    raise ResearchIntegrityError('local observation close incomplete')
        finally:
            result = await owner.aclose() if owner is not None else {
                'llm_clients_closed': await close_owned_llm(raw)}
            campaign.append('observation_closed', result)
            if (result.get('runtime_close_complete') is False or result.get('export_errors', 0) or
                    result.get('close_errors', 0) or result.get('dropped', 0) or
                    result.get('runtime_alive_after_close', False) or not result['llm_clients_closed']):
                raise ResearchIntegrityError('observation/provider close incomplete')
