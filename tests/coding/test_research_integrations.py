import importlib.util

import pytest


def test_integration_module_exists():
    assert importlib.util.find_spec('my_coding_agent.research_integrations') is not None


def test_memory_freezes_and_rejects_writes_from_holdout(tmp_path):
    from my_coding_agent.research_integrations import freeze_memory, verify_memory, record_experience
    snapshot = tmp_path / 'snapshot'
    pins = freeze_memory(None, snapshot)
    verify_memory(snapshot, pins)
    with pytest.raises(ValueError, match='train'):
        record_experience(tmp_path / 'experience', 'test', {'score': .8})
    (snapshot / 'evil').write_text('changed')
    with pytest.raises(ValueError):
        verify_memory(snapshot, pins)


def test_experience_is_local_and_no_final_feedback(tmp_path):
    from my_coding_agent.research_integrations import record_experience, freeze_memory, retrieve_experience
    evidence = {'round': 0, 'code_sha256': 'a'*64, 'score': .75,
                'report_sha256': 'b'*64, 'provenance': {'kind': 'synthetic'}}
    record_experience(tmp_path / 'memory', 'train', evidence)
    snapshot = tmp_path / 'snapshot'
    pins = freeze_memory(tmp_path / 'memory', snapshot)
    text = retrieve_experience(snapshot, pins, 'verified development result')
    assert '0.75' in text
    with pytest.raises(ValueError, match='final'):
        record_experience(tmp_path / 'memory', 'train', {**evidence, 'final': {'score': 1}})


def test_local_observer_needs_no_cloud_credentials(tmp_path):
    from my_coding_agent.research_integrations import ResearchObservation
    class Subscription:
        closed = False
        def close(self):
            self.closed = True
    class Raw:
        def add_observer(self, observer):
            self.observer = observer
            return Subscription()
    observer = ResearchObservation(tmp_path / 'calls.jsonl')
    raw = Raw()
    observer.attach(raw)
    result = observer.close()
    assert result['cloud_upload'] == 'unverified'
    assert result['local_closed']


def test_explicit_mem0_uses_existing_adapter_and_accounts_local_work(tmp_path, monkeypatch):
    import asyncio
    from my_coding_agent.research_integrations import freeze_memory, aretrieve_experience
    from my_agent_core import memory_mem0
    received = []
    class Index:
        status = {'provider': 'mem0', 'circuit_open': False, 'inflight': False}
        async def search(self, **kwargs):
            received.append(kwargs)
            return []
        async def aclose(self, timeout):
            return True
    def factory(config, *, timeout):
        assert config['profile'] == 'local_cpu_zh'
        assert timeout == 5
        return Index()
    monkeypatch.setattr(memory_mem0, 'create_mem0_index', factory)
    path = tmp_path / 'snapshot'
    pins = freeze_memory(None, path)
    text, evidence = asyncio.run(aretrieve_experience(path, pins, 'task', mode='mem0',
        mem0_config={'profile': 'local_cpu_zh', 'model_dir': str(tmp_path)}, cache_dir=tmp_path/'cache'))
    assert len(received) == 1 and text == ''
    assert evidence['mode'] == 'mem0' and evidence['retrieval_calls'] == 1
    assert evidence['provider_requests'] == 0 and evidence['provider_tokens'] == 0
    assert evidence['closed'] and evidence['cost_usd'] is None


def test_none_memory_mode_does_not_construct_sdk(tmp_path):
    import asyncio
    from my_coding_agent.research_integrations import freeze_memory, aretrieve_experience
    path = tmp_path / 'snapshot'
    pins = freeze_memory(None, path)
    text, evidence = asyncio.run(aretrieve_experience(path, pins, 'task', mode='none'))
    assert text == '' and evidence['mode'] == 'none'
