"""Seventh continuation after compat6 stopped before its first model request."""
from __future__ import annotations

from copy import deepcopy
import json

from experiments.research_validation import rsi_compat6 as previous
from my_coding_agent.research_runtime import ResearchCampaign
from my_coding_agent.rsi_branch import file_sha

ROOT, WORKTREE = previous.ROOT, previous.WORKTREE
PARENT_STUDY = previous.STUDY
STUDY = ROOT / 'rsi-study-v4-compat7'
PARENT_PROTOCOL_SHA256 = '52b5fab0ce456e099e90711b9ab897b2513ef4784ecc02d1304f50e680679dab'
PARENT_SOURCE_MANIFEST_SHA256 = (
    'ab0af6298fe72259252e467948ac457d85d12d5165dfc158787bfa1f89eca75c'
)
JOINT_LIMITS = deepcopy(previous.JOINT_LIMITS)
HISTORICAL_USAGE = deepcopy(previous.HISTORICAL_USAGE)
NEW_LIMITS = deepcopy(previous.NEW_LIMITS)
TEST_CAMPAIGNS = deepcopy(previous.TEST_CAMPAIGNS)
TEST_ORDER = list(previous.TEST_ORDER)
REMAINING_NAMES = tuple(previous.REMAINING_NAMES)


def _pin(path):
    return {'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}


def parent_evidence():
    paths = {'protocol': PARENT_STUDY / 'protocol.json',
        'source_manifest': PARENT_STUDY / 'source-pins.json',
        'STOP': PARENT_STUDY / 'STOP.json',
        'greedy_manifest': PARENT_STUDY / 'c-qsar-greedy/campaign.json',
        'greedy_journal': PARENT_STUDY / 'c-qsar-greedy/journal.jsonl',
        'greedy_run': PARENT_STUDY / 'command-evidence/run-c-qsar-greedy.json'}
    return {'protocol_sha256': PARENT_PROTOCOL_SHA256,
            'source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'pins': [{'id': key, **_pin(path)} for key, path in paths.items()]}


def verify_parent():
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    stop = json.loads((PARENT_STUDY / 'STOP.json').read_text(encoding='utf-8'))
    if (file_sha(PARENT_STUDY / 'protocol.json') != PARENT_PROTOCOL_SHA256 or
            file_sha(PARENT_STUDY / 'source-pins.json') != PARENT_SOURCE_MANIFEST_SHA256 or
            protocol.get('version') != 'rsi-study-v4-compat6' or
            stop.get('study') != 'rsi-study-v4-compat6' or stop.get('error_type') != 'RuntimeError'):
        raise RuntimeError('compat6 zero-usage STOP changed')
    usage = {}
    for spec in TEST_CAMPAIGNS:
        campaign = ResearchCampaign.load(PARENT_STUDY / spec['name'])
        budget = campaign.status()['budget']
        usage[spec['name']] = {'requests': budget['requests'], 'tokens': budget['used_tokens'],
                               'held': budget['held_tokens'], 'evaluations': budget['evaluations'],
                               'wall_seconds': budget['wall_seconds']}
        events = [row['kind'] for row in campaign.events()]
        if any(usage[spec['name']][key] for key in ('requests', 'tokens', 'held', 'evaluations')):
            raise RuntimeError('compat6 paid usage appeared before compat7')
        if events not in (['prepared'], ['prepared', 'policy_fallback_applied']):
            raise RuntimeError('compat6 campaign advanced past preparation')
    return {'verified': True, 'compat6_stop_preserved': True, 'parent_paid_requests': 0,
            'parent_paid_evaluations': 0, 'parent_usage': usage,
            'historical_usage': HISTORICAL_USAGE}


def budget_plan():
    plan = previous.budget_plan()
    if plan['historical'] != HISTORICAL_USAGE or plan['new_limits'] != NEW_LIMITS:
        raise RuntimeError('compat7 historical balance changed')
    return {**plan, 'parent_zero_usage_note': 'compat6 failed before image_verified or model dispatch'}


def assert_joint_budget(budgets):
    return previous.assert_joint_budget(budgets)


def optimizer_decision():
    return previous.optimizer_decision()


def _rewrite_path(value):
    if isinstance(value, str) and value.startswith(str(PARENT_STUDY)):
        return str(STUDY) + value[len(str(PARENT_STUDY)):]
    if isinstance(value, list):
        return [_rewrite_path(item) for item in value]
    return value


def prepare_argv(campaign):
    return [_rewrite_path(value) for value in previous.prepare_argv(campaign)]


def run_argv(name):
    return [_rewrite_path(value) for value in previous.run_argv(name)]


def final_argv(name):
    return [_rewrite_path(value) for value in previous.final_argv(name)]


def build_protocol(source_files, *, cli_command, base_url):
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    return {**protocol, 'version': 'rsi-study-v4-compat7',
        'source_pins': [{'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}
                        for path in source_files],
        'compatibility_parent_protocol_sha256': PARENT_PROTOCOL_SHA256,
        'compatibility_parent_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
        'compatibility_parent_evidence': parent_evidence(),
        'compatibility_amendment': {'previous_protocol_sha256': PARENT_PROTOCOL_SHA256,
            'previous_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'new_model_requests_at_amendment': 0,
            'scope': 'generic compat-protocol observation-version recognition; no scientific change'},
        'historical_verification': verify_parent(), 'budget_plan': budget_plan(),
        'optimizer_decision': optimizer_decision(), 'campaigns_new': TEST_CAMPAIGNS,
        'observation': {**protocol['observation'], 'readback_timeout_seconds': 180,
            'max_readonly_queries_per_gate': 8, 'provider_retries': 0},
        'new_vs_parent': [*protocol['new_vs_parent'],
            'compat6 zero-paid preparations and STOP remain immutable',
            'compat7 uses fresh campaign/task/root/call identities for five remaining arms']}
