"""Fifth compatibility continuation after compat4 failed before its first model call."""
from __future__ import annotations

from copy import deepcopy
import json

from experiments.research_validation import rsi_compat4 as previous
from my_coding_agent.research_runtime import ResearchCampaign
from my_coding_agent.rsi_branch import file_sha

ROOT, WORKTREE = previous.ROOT, previous.WORKTREE
PARENT_STUDY = previous.STUDY
STUDY = ROOT / 'rsi-study-v4-compat5'
PARENT_PROTOCOL_SHA256 = 'e0a5c0c4d31bf21c4e9e1a245ac49965ac0fc29798df05bf7f52cd57e93e0f17'
PARENT_SOURCE_MANIFEST_SHA256 = (
    '274d8d148df7347d9c4c68720ff1a6dba7373eec8fb51209645bcc92d4d36be8'
)
JOINT_LIMITS = deepcopy(previous.JOINT_LIMITS)
HISTORICAL_USAGE = deepcopy(previous.HISTORICAL_USAGE)
NEW_LIMITS = deepcopy(previous.NEW_LIMITS)
TEST_CAMPAIGNS = deepcopy(previous.TEST_CAMPAIGNS)
TEST_ORDER = list(previous.TEST_ORDER)


def _pin(path):
    return {'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}


def parent_evidence():
    paths = {'protocol': PARENT_STUDY / 'protocol.json',
        'source_manifest': PARENT_STUDY / 'source-pins.json',
        'STOP': PARENT_STUDY / 'STOP.json',
        'progress': ROOT / 'ds1-compat4-progress.json',
        'driver_smoke': PARENT_STUDY / 'driver-smoke.json',
        'qsar_fixed_manifest': PARENT_STUDY / 'c-qsar-fixed/campaign.json',
        'qsar_fixed_journal': PARENT_STUDY / 'c-qsar-fixed/journal.jsonl',
        'qsar_fixed_run': PARENT_STUDY / 'command-evidence/run-c-qsar-fixed.json'}
    return {'protocol_sha256': PARENT_PROTOCOL_SHA256,
            'source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'pins': [{'id': key, **_pin(path)} for key, path in paths.items()]}


def verify_parent():
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    stop = json.loads((PARENT_STUDY / 'STOP.json').read_text(encoding='utf-8'))
    if (file_sha(PARENT_STUDY / 'protocol.json') != PARENT_PROTOCOL_SHA256 or
            file_sha(PARENT_STUDY / 'source-pins.json') != PARENT_SOURCE_MANIFEST_SHA256 or
            protocol.get('version') != 'rsi-study-v4-compat4' or
            stop.get('study') != 'rsi-study-v4-compat4' or stop.get('error_type') != 'RuntimeError'):
        raise RuntimeError('compat4 zero-usage STOP changed')
    usage = {}
    for spec in TEST_CAMPAIGNS:
        campaign = ResearchCampaign.load(PARENT_STUDY / spec['name'])
        budget = campaign.status()['budget']
        usage[spec['name']] = {'requests': budget['requests'], 'tokens': budget['used_tokens'],
                               'held': budget['held_tokens'], 'evaluations': budget['evaluations'],
                               'wall_seconds': budget['wall_seconds']}
        if usage[spec['name']]['requests'] or usage[spec['name']]['tokens'] or usage[spec['name']]['held'] or usage[spec['name']]['evaluations']:
            raise RuntimeError('compat4 paid usage appeared before compat5 continuation')
        events = campaign.events()
        if [row['kind'] for row in events] not in (['prepared'], ['prepared', 'policy_fallback_applied']):
            raise RuntimeError('compat4 campaign advanced past prepared boundary')
    return {'verified': True, 'compat4_stop_preserved': True, 'parent_paid_requests': 0,
            'parent_paid_evaluations': 0, 'parent_usage': usage,
            'historical_usage': HISTORICAL_USAGE, 'new_campaigns_copied_as_fresh_preparations': True}


def budget_plan():
    plan = previous.budget_plan()
    if plan['historical'] != HISTORICAL_USAGE or plan['new_limits'] != NEW_LIMITS:
        raise RuntimeError('compat5 historical balance changed')
    return {**plan, 'parent_zero_usage_note': 'compat4 failed before image_verified or model dispatch'}


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
    evidence = parent_evidence()
    verification = verify_parent()
    source_pins = [{'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}
                   for path in source_files]
    return {**protocol, 'version': 'rsi-study-v4-compat5', 'source_pins': source_pins,
        'compatibility_parent_protocol_sha256': PARENT_PROTOCOL_SHA256,
        'compatibility_parent_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
        'compatibility_parent_evidence': evidence,
        'compatibility_amendment': {'previous_protocol_sha256': PARENT_PROTOCOL_SHA256,
            'previous_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'new_model_requests_at_amendment': 0,
            'scope': 'recognize compat4 protocol version in observation loader; no scientific change'},
        'historical_verification': verification, 'budget_plan': budget_plan(),
        'optimizer_decision': optimizer_decision(), 'campaigns_new': TEST_CAMPAIGNS,
        'observation': {**protocol['observation'], 'readback_timeout_seconds': 180,
            'max_readonly_queries_per_gate': 8, 'provider_retries': 0},
        'new_vs_parent': [*protocol['new_vs_parent'],
            'compat4 zero-paid STOP and prepared campaign identities remain immutable',
            'fresh compat5 task/campaign/root/call identities replace compat4 preparations',
            'eight original test coordinates and per-arm ceilings remain unchanged']}
