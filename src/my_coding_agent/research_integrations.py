"""Host-controlled experience memory and metadata observation for research.

Local MemoryStore is authoritative. Mem0 uses only the existing pinned local CPU
profile: no remote embedder or inference client can escape the parent API budget.
"""
from __future__ import annotations

import json
from pathlib import Path
import time

from .rsi_branch import copy_tree, inventory, plain_path


def freeze_memory(source, destination):
    destination = plain_path(destination).resolve()
    if source is None:
        destination.mkdir(parents=True)
    else:
        copy_tree(plain_path(source).resolve(), destination)
    return inventory(destination)


def verify_memory(path, pins):
    if inventory(path) != pins:
        raise ValueError('frozen memory snapshot changed')


def retrieve_experience(path, pins, query):
    from my_agent_core.memory import MemoryStore
    verify_memory(path, pins)
    memory = MemoryStore(Path(path))
    memory.load_from_disk()
    result = memory.retrieve(query, budget_chars=1200) or ''
    verify_memory(path, pins)
    return result


async def aretrieve_experience(path, pins, query, *, mode='lexical', mem0_config=None, cache_dir=None):
    """Account local SDK/embedding work separately from provider usage and money."""
    from my_agent_core.memory import MemoryStore
    verify_memory(path, pins)
    if mode not in ('none', 'lexical', 'mem0'):
        raise ValueError('unknown memory mode')
    started = time.monotonic()
    evidence = {'mode': mode, 'provider_requests': 0, 'provider_tokens': 0, 'cost_usd': None,
                'cost_basis': 'local_cpu_work_no_provider_invoice', 'retrieval_calls': 0,
                'embedding_calls': 0, 'closed': True}
    if mode == 'none':
        return '', {**evidence, 'wall_seconds': time.monotonic() - started}
    index = None
    try:
        if mode == 'mem0':
            from my_agent_core.memory_mem0 import create_mem0_index
            config = dict(mem0_config or {})
            if (config.get('profile') != 'local_cpu_zh' or
                    set(config) - {'profile', 'model_dir', 'threads'} or cache_dir is None):
                raise ValueError('research Mem0 requires explicit pinned local CPU profile and owned cache')
            config['storage_dir'] = str(Path(cache_dir).resolve())
            build_started = time.monotonic()
            index = create_mem0_index(config, timeout=5)
            evidence['construction_seconds'] = time.monotonic() - build_started
            embedder = getattr(getattr(index, 'memory', None), 'embedding_model', None)
            if embedder is not None:
                embed = embedder.embed
                def counted_embed(*args, **kwargs):
                    evidence['embedding_calls'] += 1
                    return embed(*args, **kwargs)
                embedder.embed = counted_embed
        class TrackedIndex:
            async def search(self, **kwargs):
                try:
                    candidates = await index.search(**kwargs)
                    current = {r['identity']: r for r in kwargs['records']}
                    evidence['valid_semantic_candidates'] = sum(
                        c.get('identity') in current and c.get('scope') == kwargs['scope'] and
                        c.get('revision') == current[c['identity']]['revision'] for c in candidates)
                    return candidates
                except BaseException:
                    evidence['semantic_error'] = True
                    raise
        memory = MemoryStore(Path(path), memory_index=TrackedIndex() if index is not None else None)
        memory.load_from_disk()
        evidence['retrieval_calls'] = 1
        result = await memory.aretrieve(query, budget_chars=1200) or ''
        if index is not None:
            evidence['sdk_status'] = dict(index.status)
            evidence['lexical_fallback'] = not evidence.get('valid_semantic_candidates', 0)
            evidence['semantic_status'] = 'fallback_or_empty' if evidence['lexical_fallback'] else 'verified_candidates'
        verify_memory(path, pins)
    finally:
        if index is not None:
            evidence['closed'] = await index.aclose(timeout=5)
        evidence['wall_seconds'] = time.monotonic() - started
    if not evidence['closed']:
        raise RuntimeError('local Mem0 worker cleanup incomplete; recovery cannot replay')
    return result, evidence


def record_experience(path, split, evidence):
    from my_agent_core.memory import MemoryStore
    from .research_runtime import digest
    if split != 'train':
        raise ValueError('experience writes require strategy train split')
    if any('final' in key.lower() for key in evidence):
        raise ValueError('final feedback must never enter experience memory')
    fields = {'round', 'code_sha256', 'score', 'report_sha256', 'provenance'}
    if not fields <= evidence.keys():
        raise ValueError('verified development evidence required')
    value = {key: evidence[key] for key in fields}
    for key in ('task_context', 'method_context'):
        if key in evidence:
            value[key] = evidence[key]
    value['evidence_sha256'] = digest(value)
    value['kind'] = 'verified development result; host dev utility, not final success'
    memory = MemoryStore(Path(path))
    memory.load_from_disk()
    memory.remember('research-' + value['evidence_sha256'], json.dumps(value, sort_keys=True),
                    subject='research', source='host-development-evaluator')


class ResearchObservation:
    """Reuse the existing bounded observer; cloud status never assumes delivery."""
    def __init__(self, path, *, cloud=False):
        from my_agent_llm.observation_export import BoundedExporter, JsonlSink
        from my_agent_llm.observation_runtime import ObservationRuntime, create_observation_runtime
        self.local = ObservationRuntime(BoundedExporter(JsonlSink(path)))
        self.cloud = None
        self.cloud_status = 'unverified'
        if cloud:
            import os
            from .settings import load_settings
            settings = load_settings().observation
            # Resolve the configured environment names but never export their values.
            public = os.environ.get(settings.public_key_env)
            secret = os.environ.get(settings.secret_key_env)
            if public and secret:
                self.cloud = create_observation_runtime(enabled=True, public_key=public, secret_key=secret,
                    base_url=settings.base_url, capacity=settings.capacity, network_timeout=settings.network_timeout)
                self.cloud_status = 'configured_delivery_unverified'

    def attach(self, raw):
        self.local.attach(raw)
        if self.cloud is not None:
            self.cloud.attach(raw)

    def close(self):
        local_ok = self.local.close(timeout=5)
        cloud_ok = self.cloud.close(timeout=5) if self.cloud is not None else None
        return {'local_closed': local_ok, 'local_dropped': self.local.dropped,
                'local_export_errors': self.local.export_errors, 'cloud_closed': cloud_ok,
                'cloud_upload': self.cloud_status}
