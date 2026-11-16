"""RSI raw observer integration; no real SDK/network/provider calls."""
import asyncio
from contextlib import contextmanager
import json

import pytest

from experiments.benchmark_live.observation import ExperimentLLMOwner, SessionScopeSink
from experiments.research_validation.rsi_observation import RemoteReadbackGate, match_remote
from my_agent_llm.observation import CallEnd
from my_agent_llm.observation_export import BoundedExporter
from my_agent_llm.observation_runtime import ObservationRuntime
from my_coding_agent.research_runtime import ResearchCampaign, ResearchBudget, ResearchIntegrityError, canonical
from my_coding_agent.rsi_runtime import SharedLLM, execution_scope
from tests.coding.test_research_runtime import campaign, fake_evaluator, scripted_raw
from tests.experiments.test_experiment_observation import Raw


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    from my_coding_agent.research_runtime import credential_environment
    for name in credential_environment():
        monkeypatch.delenv(name)


def owner_at(raw, campaign_id='offline-campaign'):
    rows, scopes = [], []
    @contextmanager
    def propagate(**kw):
        scopes.append(kw)
        yield
    scoped = SessionScopeSink(rows.append, campaign_id=campaign_id, propagate=propagate)
    owner = ExperimentLLMOwner(raw, runtime=ObservationRuntime(BoundedExporter(scoped)), scope_sink=scoped)
    return owner, rows, scopes


def observed_campaign(tmp_path):
    old = campaign(tmp_path)
    path = tmp_path / 'protocol.json'
    path.write_bytes(canonical({'version': 'rsi-study-v4-continuation', 'source_pins': [],
        'observation': {'readback_timeout_seconds': 1}}))
    from my_coding_agent.rsi_branch import file_sha
    new = ResearchCampaign.prepare(old.task, tmp_path / 'observed', image=old.manifest['image'],
        limits=old.manifest['limits'], memory_mode='none',
        observation_contract='langfuse_campaign_v1',
        observation_protocol={'file': str(path), 'sha256': file_sha(path)})
    return new


def remote_rows(c, root_id, calls):
    import hashlib
    return [{'id': str(i), 'sessionId': c.manifest['campaign_id'],
        'traceId': hashlib.sha256(root_id.encode()).hexdigest()[:32], 'endTime': '2026-11-14T00:00:00Z',
        'usageDetails': {'input': 2, 'output': 1, 'total': 3}, 'model': 'offline-model',
        'metadata': {'campaign': c.manifest['campaign_id'], 'arm': c.manifest['arm'],
            'stage': 'train', 'task_id': c.task.manifest_sha256, 'root_id': root_id,
            'call_id': call.call_id, 'parent_id': call.parent_id, 'status': call.status}}
        for i, call in enumerate(calls.values())]


def test_same_root_multiple_calls_have_paired_scope_and_exactly_one_budget(tmp_path):
    c = campaign(tmp_path)
    budget = ResearchBudget(c)
    raw = Raw({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3})
    owner, records, scopes = owner_at(raw)
    llm = SharedLLM(raw, budget)
    async def run():
        for branch in (0, 1):
            with owner.observe(budget.root_id, arm='rsi-training', stage='train', task_id='opaque-task'):
                with execution_scope(round=0, branch=branch, attempt=0, stage='attempt'):
                    for _ in range(2):
                        await llm.achat([], max_tokens=8)
        return await owner.aclose()
    closed = asyncio.run(run())
    assert closed['export_errors'] == closed['dropped'] == 0
    assert closed['runtime_close_complete'] and closed['llm_clients_closed']
    assert len(budget.records) == raw.calls == 4
    assert budget.snapshot()['used_tokens'] == 12
    assert len(records) == len(scopes) == 8
    assert {r.root_id for r in records} == {budget.root_id}
    for call_id in {r.call_id for r in records}:
        pair = [r for r in records if r.call_id == call_id]
        assert len(pair) == 2 and isinstance(pair[-1], CallEnd)
        assert pair[0].parent_id == pair[-1].parent_id
    assert {json.loads(r.parent_id)['branch'] for r in records} == {0, 1}
    assert all(s['session_id'] == 'offline-campaign' and s['metadata']['arm'] == 'rsi-training' for s in scopes)
    assert raw.closes == 1 and not raw.observers


def test_observer_scopes_do_not_cross_campaign_session(tmp_path):
    scopes = []
    for index in range(2):
        raw = Raw({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3})
        owner, _, contexts = owner_at(raw, campaign_id=f'campaign-{index}')
        async def run():
            from my_agent_llm.observation import observation_context
            with owner.observe(f'root-{index}', arm=f'arm-{index}', stage='test', task_id=f'task-{index}'):
                with observation_context(parent_id='{}'):
                    await raw.achat([])
            await owner.aclose()
        asyncio.run(run())
        scopes.extend(contexts)
    assert [s['session_id'] for s in scopes] == ['campaign-0'] * 2 + ['campaign-1'] * 2
    assert [s['metadata']['arm'] for s in scopes] == ['arm-0'] * 2 + ['arm-1'] * 2


def test_required_owner_cannot_silently_fall_back_to_unobserved_run(tmp_path):
    c = observed_campaign(tmp_path)
    raw = scripted_raw()
    with pytest.raises(ResearchIntegrityError, match='required raw Langfuse'):
        asyncio.run(c.run(raw, evaluator=fake_evaluator()))
    assert raw.calls == 0


@pytest.mark.parametrize('present', [True, False])
def test_first_two_ordinary_calls_readback_before_third_reservation(tmp_path, present):
    c = observed_campaign(tmp_path)
    raw = Raw({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3})
    owner, _, _ = owner_at(raw, c.manifest['campaign_id'])
    queries = []
    protocol = json.loads((tmp_path / 'protocol.json').read_text())
    def query(root_id, remaining):
        queries.append((raw.calls, len(budget.records)))
        return remote_rows(c, root_id, gate.calls) if present else []
    gate = RemoteReadbackGate(c, owner, protocol, {}, query=query, wait=lambda _: None)
    c.before_request = gate
    budget = ResearchBudget(c)
    llm = SharedLLM(raw, budget)
    async def run():
        try:
            with owner.observe(budget.root_id, arm=c.manifest['arm'], stage='train', task_id=c.task.manifest_sha256):
                with execution_scope(round=0, branch=0, attempt=0, stage='attempt'):
                    await llm.achat([], max_tokens=8)
                    await llm.achat([], max_tokens=8)
                    assert not queries
                    if present:
                        await llm.achat([], max_tokens=8)
                    else:
                        with pytest.raises(ResearchIntegrityError, match='not remotely verified'):
                            await llm.achat([], max_tokens=8)
        finally:
            gate.close()
            await owner.aclose()
    asyncio.run(run())
    assert all(q == (2, 2) for q in queries)
    assert raw.calls == len(budget.records) == (3 if present else 2)
    assert gate.verified is present
    assert (c.root / 'observation-readback-0001-0002.json').exists() is present
    assert budget.snapshot()['used_tokens'] == (9 if present else 6)


@pytest.mark.parametrize('wrong', ['sessionId', 'traceId', 'arm', 'stage', 'task_id', 'parent_id', 'usage'])
def test_remote_gate_rejects_incorrect_scope_or_usage(tmp_path, wrong):
    c = observed_campaign(tmp_path)
    from my_agent_llm.observation import CallEnd
    call = CallEnd('call', 'root', '{"branch":0,"attempt":1}', 'agent', 'offline', 'offline-model',
                   'achat', 0, 'ok', 1, {'total_tokens': 3}, None)
    calls = {'call': call}
    rows = remote_rows(c, 'root', calls)
    if wrong in ('sessionId', 'traceId'):
        rows[0][wrong] = 'wrong'
    elif wrong == 'usage':
        rows[0]['usageDetails']['total'] = 4
    else:
        rows[0]['metadata'][wrong] = '{}' if wrong == 'parent_id' else 'wrong'
    assert match_remote(rows, calls, campaign=c, root_id='root') == {}


def test_unknown_usage_still_stops_with_observer_and_closes_clients(tmp_path):
    c = campaign(tmp_path)
    budget = ResearchBudget(c)
    raw = Raw(None)
    owner, rows, _ = owner_at(raw)
    from my_coding_agent.rsi_runtime import RsiBudgetExceeded
    async def run():
        try:
            with owner.observe(budget.root_id, arm='rsi-training', stage='train', task_id='opaque-task'):
                with pytest.raises(RsiBudgetExceeded):
                    await SharedLLM(raw, budget).achat([], max_tokens=8)
        finally:
            await owner.aclose()
    asyncio.run(run())
    assert budget.blocked and budget.snapshot()['held_tokens'] > 0
    assert budget.records[0]['usage'] is None
    assert len([r for r in rows if isinstance(r, CallEnd)]) == 1
    assert raw.calls == raw.closes == 1


def test_later_real_attempt_is_checked_without_extra_model_probe(tmp_path):
    c = observed_campaign(tmp_path)
    raw = Raw({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3})
    owner, _, _ = owner_at(raw, c.manifest['campaign_id'])
    queries = []
    def query(root_id, remaining):
        queries.append(raw.calls)
        return remote_rows(c, root_id, gate.calls)
    protocol = json.loads((tmp_path / 'protocol.json').read_text())
    gate = RemoteReadbackGate(c, owner, protocol, {}, query=query, wait=lambda _: None)
    c.before_request = gate
    budget = ResearchBudget(c)
    llm = SharedLLM(raw, budget)
    async def run():
        try:
            with owner.observe(budget.root_id, arm=c.manifest['arm'], stage='train', task_id=c.task.manifest_sha256):
                with execution_scope(round=0, branch=0, attempt=0, stage='attempt'):
                    for _ in range(3):
                        await llm.achat([], max_tokens=8)
                with execution_scope(round=0, branch=1, attempt=0, stage='attempt'):
                    await llm.achat([], max_tokens=8)
                    await llm.achat([], max_tokens=8)
        finally:
            gate.close()
            await owner.aclose()
    asyncio.run(run())
    assert raw.calls == len(budget.records) == 5
    assert queries == [2, 4]
    assert {json.loads(c.parent_id)['branch'] for c in gate.calls.values()} == {0, 1}
    assert len(gate.verified_ids) == 3


def test_required_health_failure_stops_after_settled_call_before_next_reservation(tmp_path, monkeypatch):
    from experiments.benchmark_live.observation import ObservationRequiredError
    c = observed_campaign(tmp_path)
    raw = Raw({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3})
    owner, _, _ = owner_at(raw, c.manifest['campaign_id'])
    owner.required = True
    gate = RemoteReadbackGate(c, owner, json.loads((tmp_path / 'protocol.json').read_text()), {}, query=lambda *_: [])
    c.before_request = gate
    budget = ResearchBudget(c)
    async def run():
        try:
            with owner.observe(budget.root_id, arm=c.manifest['arm'], stage='train', task_id=c.task.manifest_sha256):
                await SharedLLM(raw, budget).achat([], max_tokens=8)
                def lost(**kwargs):
                    raise ObservationRequiredError('offline export lost')
                monkeypatch.setattr(owner, 'flush_and_check', lost)
                with pytest.raises(ObservationRequiredError):
                    await SharedLLM(raw, budget).achat([], max_tokens=8)
        finally:
            gate.close()
            await owner.aclose()
    asyncio.run(run())
    assert raw.calls == len(budget.records) == 1
    assert budget.snapshot()['used_tokens'] == 3
    assert budget.snapshot()['held_tokens'] == 0


def test_remote_requires_distinct_observation_ids_for_distinct_calls(tmp_path):
    c = observed_campaign(tmp_path)
    from types import SimpleNamespace
    calls = {str(i): SimpleNamespace(call_id=str(i), parent_id='{}', status='ok',
        usage={'total_tokens': 3}) for i in range(2)}
    rows = remote_rows(c, 'offline-root', calls)
    rows[1]['id'] = rows[0]['id']
    assert len(match_remote(rows, calls, campaign=c, root_id='offline-root')) == 1


def test_campaign_assembly_required_raw_owner_env_scrub_and_single_budget(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    from my_agent_llm import Response
    from experiments.research_validation import rsi_observation as integration
    from my_coding_agent import research_cli
    from my_coding_agent.research_runtime import credential_environment
    c = observed_campaign(tmp_path)
    class AgentRaw(Raw):
        async def achat(self, messages, **kwargs):
            await super().achat(messages, **kwargs)
            return Response(model=self.model, content='done', usage=self.usage)
    raw = AgentRaw({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3})
    raw.config.max_tokens = 64
    protocol = {'version': 'rsi-study-v4-continuation', 'source_pins': [], 'observation': {
        'sdk_version': '5.2.0', 'cli_pins': {}, 'base_url': 'https://offline.invalid',
        'readback_timeout_seconds': 1}}
    monkeypatch.setattr(integration, 'load_frozen_protocol', lambda _: protocol)
    monkeypatch.setattr(integration, 'version', lambda _: '5.2.0')
    for key, value in {'LANGFUSE_PUBLIC_KEY': 'offline-public', 'LANGFUSE_SECRET_KEY': 'offline-secret',
            'LANGFUSE_BASE_URL': 'https://offline.invalid'}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(integration, 'load_observation_environment', lambda: {
        k: os.environ[k] for k in ('LANGFUSE_PUBLIC_KEY', 'LANGFUSE_SECRET_KEY', 'LANGFUSE_BASE_URL')})
    monkeypatch.setattr(research_cli, 'construct_llm', lambda *a, **kw: raw)
    owners = []
    def factory(actual_raw, **kwargs):
        assert actual_raw is raw and kwargs['required'] is True
        assert os.environ.get('LANGFUSE_PUBLIC_KEY') and os.environ.get('LANGFUSE_SECRET_KEY')
        owner, _, _ = owner_at(raw, c.manifest['campaign_id'])
        owner.required = True
        owners.append(owner)
        return owner
    monkeypatch.setattr(ExperimentLLMOwner, 'from_environment', factory)
    monkeypatch.setattr(RemoteReadbackGate, '_query', lambda self, root_id, _: remote_rows(c, root_id, self.calls))
    original_run = c.run
    async def run(actual_raw, **kwargs):
        assert not credential_environment()
        assert 'LANGFUSE_SECRET_KEY' not in os.environ and 'LANGFUSE_PUBLIC_KEY' not in os.environ
        assert actual_raw is raw
        return await original_run(actual_raw, evaluator=fake_evaluator(), **kwargs)
    monkeypatch.setattr(c, 'run', run)
    result = asyncio.run(integration.run_observed_campaign(SimpleNamespace(credential_env='RESEARCH_API_KEY',
        auth_provider=None, auth_profile=None, pause_after_round=None), c))
    assert result['budget']['requests'] == raw.calls >= 2
    assert result['budget']['used_tokens'] == raw.calls * 3
    assert raw.closes == 1 and len(owners) == 1 and owners[0].required
    assert any(e['kind'] == 'observation_readback_verified' for e in c.events())


@pytest.mark.parametrize('remote_value', [{}, '{}'])
def test_cli_parent_id_accepts_object_or_json_string_only(tmp_path, remote_value):
    from types import SimpleNamespace
    c = observed_campaign(tmp_path)
    calls = {'one': SimpleNamespace(call_id='one', parent_id='{}', status='ok', usage={'total_tokens': 3})}
    rows = remote_rows(c, 'offline-root', calls)
    rows[0]['metadata']['parent_id'] = remote_value
    assert len(match_remote(rows, calls, campaign=c, root_id='offline-root')) == 1


@pytest.mark.parametrize('remote_value', [None, [], 4, True, '[]', 'null', '{broken', '{"x":NaN}'])
def test_cli_parent_id_rejects_other_types_and_malformed_json(tmp_path, remote_value):
    from types import SimpleNamespace
    c = observed_campaign(tmp_path)
    calls = {'one': SimpleNamespace(call_id='one', parent_id='{}', status='ok', usage={'total_tokens': 3})}
    rows = remote_rows(c, 'offline-root', calls)
    rows[0]['metadata']['parent_id'] = remote_value
    with pytest.raises(ResearchIntegrityError, match='parent_id'):
        match_remote(rows, calls, campaign=c, root_id='offline-root')


@pytest.mark.parametrize('recovers', [True, False])
def test_readonly_timeout_polling_is_bounded_and_never_retries_model(tmp_path, recovers):
    import subprocess
    c = observed_campaign(tmp_path)
    raw = Raw({'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3})
    owner, _, _ = owner_at(raw, c.manifest['campaign_id'])
    queries = []
    def query(root_id, remaining):
        queries.append(raw.calls)
        if len(queries) == 1 or not recovers:
            raise subprocess.TimeoutExpired('offline-readonly', 0)
        return remote_rows(c, root_id, gate.calls)
    gate = RemoteReadbackGate(c, owner, json.loads((tmp_path / 'protocol.json').read_text()), {},
        query=query, wait=lambda _: None)
    c.before_request = gate
    budget = ResearchBudget(c)
    async def run():
        try:
            with owner.observe(budget.root_id, arm=c.manifest['arm'], stage='train', task_id=c.task.manifest_sha256):
                await SharedLLM(raw, budget).achat([], max_tokens=8)
                await SharedLLM(raw, budget).achat([], max_tokens=8)
                if recovers:
                    await SharedLLM(raw, budget).achat([], max_tokens=8)
                else:
                    with pytest.raises(ResearchIntegrityError, match='deadline'):
                        await SharedLLM(raw, budget).achat([], max_tokens=8)
        finally:
            gate.close()
            await owner.aclose()
    asyncio.run(run())
    assert queries == [2] * (2 if recovers else 3)
    assert raw.calls == len(budget.records) == (3 if recovers else 2)


def test_python_metadata_transport_preserves_fixed_endpoint_fields_and_credentials(monkeypatch):
    import base64
    import hashlib
    import io
    from urllib.parse import parse_qs, urlparse
    from experiments.research_validation import rsi_observation as integration
    gate = object.__new__(RemoteReadbackGate)
    gate.config = {'base_url': 'https://offline.invalid', 'readback_request_timeout_seconds': 20}
    gate.credential_env = {'LANGFUSE_PUBLIC_KEY': 'offline-public', 'LANGFUSE_SECRET_KEY': 'offline-secret'}
    class Response(io.BytesIO):
        status = 200
    seen = []
    def request(req, *, timeout):
        seen.append((req, timeout))
        return Response(b'{"data":[{"id":"one","metadata":{"parent_id":"{}"}}]}')
    monkeypatch.setattr(integration.urllib.request, 'urlopen', request)
    assert gate._query('offline-root', 24) == [{'id': 'one', 'metadata': {'parent_id': '{}'}}]
    req, timeout = seen[0]
    parsed = urlparse(req.full_url)
    assert parsed.path == '/api/public/v2/observations'
    assert parse_qs(parsed.query) == {'traceId': [hashlib.sha256(b'offline-root').hexdigest()[:32]],
        'fields': ['core,basic,model,usage,metadata'], 'expandMetadata': ['parent_id'], 'limit': ['100']}
    assert req.get_header('Authorization') == 'Basic ' + base64.b64encode(b'offline-public:offline-secret').decode()
    assert timeout == 20
    gate._query('offline-root', 3)
    assert seen[1][1] == 3


@pytest.mark.parametrize('failure', ['timeout', 'network', 'http', 'malformed', 'shape'])
def test_python_metadata_transport_fails_closed_without_error_body(monkeypatch, failure):
    import io
    import subprocess
    from urllib.error import URLError
    from experiments.research_validation import rsi_observation as integration
    gate = object.__new__(RemoteReadbackGate)
    gate.config = {'base_url': 'https://offline.invalid', 'readback_request_timeout_seconds': 20}
    gate.credential_env = {'LANGFUSE_PUBLIC_KEY': 'offline-public', 'LANGFUSE_SECRET_KEY': 'offline-secret'}
    class Response(io.BytesIO):
        status = 502 if failure == 'http' else 200
    def request(req, **kwargs):
        if failure == 'timeout':
            raise URLError(TimeoutError('private-error-body'))
        if failure == 'network':
            raise URLError(OSError('private-error-body'))
        return Response(b'{"data":{}}' if failure == 'shape' else b'private-error-body')
    monkeypatch.setattr(integration.urllib.request, 'urlopen', request)
    expected = subprocess.TimeoutExpired if failure == 'timeout' else ResearchIntegrityError
    with pytest.raises(expected) as caught:
        gate._query('offline-root', 8)
    assert 'private-error-body' not in str(caught.value)
