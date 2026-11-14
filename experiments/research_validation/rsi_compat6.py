"""Sixth continuation: remaining test arms after a second provider-unknown stop."""
from __future__ import annotations

from copy import deepcopy
import json

from experiments.research_validation import rsi_compat5 as previous
from experiments.research_validation import rsi_compat4 as scientific
from my_coding_agent.research_continuation import _ArchivedCampaign, _budget_usage
from my_coding_agent.rsi_branch import file_sha

ROOT, WORKTREE = previous.ROOT, previous.WORKTREE
PARENT_STUDY = previous.STUDY
STUDY = ROOT / 'rsi-study-v4-compat6'
PARENT_PROTOCOL_SHA256 = 'f1ff2264be620682949275f88d53c9d28e6f8e613613c21cf54a762d39564762'
PARENT_SOURCE_MANIFEST_SHA256 = (
    'ecdf8246774324d6695d38fdfb435912894fbdf74b19e27dbaabd3e2f29f6242'
)
JOINT_LIMITS = deepcopy(previous.JOINT_LIMITS)
COMPLETED_NAMES = ('c-qsar-fixed', 'c-qsar-random')
REMAINING_NAMES = ('c-qsar-greedy', 'c-autompg-fixed', 'c-autompg-random',
                   'c-autompg-frozen', 'c-autompg-greedy')
TEST_ORDER = list(REMAINING_NAMES)
TEST_CAMPAIGNS = [deepcopy(campaign) for campaign in previous.TEST_CAMPAIGNS
                  if campaign['name'] in REMAINING_NAMES]
if [campaign['name'] for campaign in TEST_CAMPAIGNS] != TEST_ORDER:
    raise RuntimeError('compat6 remaining test coordinates changed')


def _campaign_usage(name):
    from pathlib import Path
    root = PARENT_STUDY / name
    manifest = json.loads((root / 'campaign.json').read_text(encoding='utf-8'))
    old = _ArchivedCampaign(root, manifest)
    old.source_archive = PARENT_STUDY / 'source-frozen'
    old.source_file_map = {}
    pins = json.loads((PARENT_STUDY / 'source-pins.json').read_text(encoding='utf-8'))
    for index, pin in enumerate(pins):
        filename = Path(pin['path']).name
        artifact = old.source_archive / f'{index:02d}-{filename}'
        if file_sha(artifact) != pin['sha256'] or artifact.stat().st_size != pin['bytes']:
            raise RuntimeError('compat5 frozen source archive changed')
        if filename in manifest['implementation'] and pin['sha256'] == manifest['implementation'][filename]:
            old.source_file_map[filename] = artifact
    old.verify()
    return old, _budget_usage(old)


def historical_usage():
    usages = {name: _campaign_usage(name)[1] for name in (*COMPLETED_NAMES, 'c-qsar-frozen')}
    new = {'requests': sum(row['requests'] for row in usages.values()),
        'settled_requests': sum(row['settled_requests'] for row in usages.values()),
        'unknown_requests': sum(row['unknown_requests'] for row in usages.values()),
        'settled_tokens': sum(row['settled_tokens'] for row in usages.values()),
        'existing_reservation_occupancy': sum(row['reservation_occupancy'] for row in usages.values()),
        'evaluations': sum(row['evaluations'] for row in usages.values()),
        'active_seconds': sum(row['active_seconds'] for row in usages.values()), 'campaigns': usages}
    new['accounting_units'] = new['settled_tokens'] + new['existing_reservation_occupancy']
    prior = deepcopy(scientific.HISTORICAL_USAGE)
    result = {'requests': prior['requests'] + new['requests'],
        'settled_requests': prior['settled_requests'] + new['settled_requests'],
        'unknown_requests': prior['unknown_requests'] + new['unknown_requests'],
        'settled_tokens': prior['settled_tokens'] + new['settled_tokens'],
        'existing_reservation_occupancy': prior['existing_reservation_occupancy'] + new['existing_reservation_occupancy'],
        'accounting_units': prior['accounting_units'] + new['accounting_units'],
        'evaluations': prior['evaluations'] + new['evaluations'],
        'active_seconds': prior['active_seconds'] + new['active_seconds'],
        'actual_total_tokens': None, 'actual_unknown_tokens': None, 'actual_total_usd': None,
        'reservation_note': prior['reservation_note'], 'compat5_usage': new}
    return result


HISTORICAL_USAGE = historical_usage()
NEW_LIMITS = {key: JOINT_LIMITS[key] - HISTORICAL_USAGE[key]
              for key in ('requests', 'accounting_units', 'evaluations', 'active_seconds')}


def _pin(path):
    return {'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}


def parent_evidence():
    paths = {'protocol': PARENT_STUDY / 'protocol.json',
        'source_manifest': PARENT_STUDY / 'source-pins.json',
        'STOP': PARENT_STUDY / 'STOP.json',
        'fixed_manifest': PARENT_STUDY / 'c-qsar-fixed/campaign.json',
        'fixed_journal': PARENT_STUDY / 'c-qsar-fixed/journal.jsonl',
        'random_manifest': PARENT_STUDY / 'c-qsar-random/campaign.json',
        'random_journal': PARENT_STUDY / 'c-qsar-random/journal.jsonl',
        'frozen_manifest': PARENT_STUDY / 'c-qsar-frozen/campaign.json',
        'frozen_journal': PARENT_STUDY / 'c-qsar-frozen/journal.jsonl',
        'frozen_result': PARENT_STUDY / 'c-qsar-frozen/rounds/0000/result.json'}
    return {'protocol_sha256': PARENT_PROTOCOL_SHA256,
            'source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'pins': [{'id': key, **_pin(path)} for key, path in paths.items()]}


def verify_parent():
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    stop = json.loads((PARENT_STUDY / 'STOP.json').read_text(encoding='utf-8'))
    if (file_sha(PARENT_STUDY / 'protocol.json') != PARENT_PROTOCOL_SHA256 or
            file_sha(PARENT_STUDY / 'source-pins.json') != PARENT_SOURCE_MANIFEST_SHA256 or
            protocol.get('version') != 'rsi-study-v4-compat5' or
            stop.get('study') != 'rsi-study-v4-compat5' or stop.get('error_type') != 'RuntimeError'):
        raise RuntimeError('compat5 STOP changed')
    usage = HISTORICAL_USAGE['compat5_usage']['campaigns']
    if any(usage[name]['unknown_requests'] for name in COMPLETED_NAMES):
        raise RuntimeError('compat5 completed arm acquired unknown usage')
    if (usage['c-qsar-frozen']['unknown_requests'] != 1 or
            usage['c-qsar-frozen']['reservation_occupancy'] != 87839 or
            usage['c-qsar-frozen']['evaluations'] != 3):
        raise RuntimeError('compat5 frozen unknown boundary changed')
    return {'verified': True, 'compat5_stop_preserved': True,
            'completed_arms': list(COMPLETED_NAMES), 'failed_arm': 'c-qsar-frozen',
            'remaining_arms': list(REMAINING_NAMES), 'no_attempt_replay': True,
            'unknown_requests_total': HISTORICAL_USAGE['unknown_requests'],
            'historical_usage': HISTORICAL_USAGE}


def budget_plan():
    ceilings = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for campaign in TEST_CAMPAIGNS:
        for out, key in (('requests', 'max_requests'), ('accounting_units', 'max_tokens'),
                         ('evaluations', 'max_evaluations'), ('active_seconds', 'max_seconds')):
            ceilings[out] += campaign['limits'][key]
    combined = {key: HISTORICAL_USAGE[key] + ceilings[key] for key in ceilings}
    if any(ceilings[key] > NEW_LIMITS[key] or combined[key] > JOINT_LIMITS[key] for key in ceilings):
        raise RuntimeError('compat6 remaining arms exceed budget')
    return {'joint_limits': JOINT_LIMITS, 'historical': HISTORICAL_USAGE, 'new_limits': NEW_LIMITS,
        'new_campaign_ceilings': ceilings, 'combined_ceiling': combined,
        'scope': 'five remaining test/final arms; QSAR frozen failure preserved and not rerun'}


def assert_joint_budget(budgets):
    new = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for budget in budgets:
        if budget['blocked'] or not budget['usage_complete'] or budget['held_tokens']:
            raise RuntimeError('new unknown/inflight usage permanently stops compat6')
        for out, key in (('requests', 'requests'), ('accounting_units', 'used_tokens'),
                         ('evaluations', 'evaluations'), ('active_seconds', 'wall_seconds')):
            new[out] += budget[key]
    combined = {key: HISTORICAL_USAGE[key] + new[key] for key in new}
    if any(new[key] > NEW_LIMITS[key] or combined[key] > JOINT_LIMITS[key] for key in new):
        raise RuntimeError('compat6 joint budget ceiling exceeded')
    return {'historical': HISTORICAL_USAGE, 'new': new,
        'combined': {**combined, 'actual_total_tokens': None, 'actual_total_usd': None},
        'remaining_authorized': {key: NEW_LIMITS[key] - new[key] for key in new}}


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
    return {**protocol, 'version': 'rsi-study-v4-compat6',
        'source_pins': [{'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}
                        for path in source_files],
        'compatibility_parent_protocol_sha256': PARENT_PROTOCOL_SHA256,
        'compatibility_parent_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
        'compatibility_parent_evidence': parent_evidence(),
        'compatibility_amendment': {'previous_protocol_sha256': PARENT_PROTOCOL_SHA256,
            'previous_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'new_model_requests_at_amendment': 0,
            'scope': 'continue five unstarted arms after QSAR frozen provider-unknown STOP'},
        'historical_verification': verify_parent(), 'budget_plan': budget_plan(),
        'optimizer_decision': optimizer_decision(), 'campaigns_new': TEST_CAMPAIGNS,
        'observation': {**protocol['observation'], 'readback_timeout_seconds': 180,
            'max_readonly_queries_per_gate': 8, 'provider_retries': 0},
        'new_vs_parent': [*protocol['new_vs_parent'],
            'fixed and random completed QSAR arms remain immutable',
            'QSAR frozen unknown request and reservation are retained but not replayed',
            'five unstarted arms use fresh compat6 identities',
            'AutomPG retains all four original arm coordinates for direct comparison']}
