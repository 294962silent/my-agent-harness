"""Explicit test-only continuation after compat3 provider-unknown STOP."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from experiments.research_validation import rsi_compat3 as previous
from my_coding_agent.research_continuation import _ArchivedCampaign, _budget_usage
from my_coding_agent.rsi_branch import file_sha

ROOT, WORKTREE = previous.ROOT, previous.WORKTREE
PARENT_STUDY = previous.STUDY
STUDY = ROOT / 'rsi-study-v4-compat4'
PARENT_PROTOCOL_SHA256 = 'aeb6fd636029cf7459ed76a7b0aff797acb06f9a7fc77faa60dd232a7fe2adef'
PARENT_SOURCE_MANIFEST_SHA256 = (
    'e23cfb2758009e27c4913c06f9a121beb03c73c9d60b1569c73ab918494d88fc'
)
JOINT_LIMITS = deepcopy(previous.JOINT_LIMITS)
PRE_COMPAT3_HISTORICAL = deepcopy(previous.HISTORICAL_USAGE)
TEST_ORDER = list(previous.TEST_ORDER)


def _campaign_usage(name):
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
            raise RuntimeError('compat3 frozen source archive changed')
        if filename in manifest['implementation'] and pin['sha256'] == manifest['implementation'][filename]:
            old.source_file_map[filename] = artifact
    old.verify()
    return old, _budget_usage(old)


def historical_usage():
    yacht, yacht_usage = _campaign_usage('c-yacht-train')
    wine, wine_usage = _campaign_usage('c-wine-val')
    new = {'requests': yacht_usage['requests'] + wine_usage['requests'],
        'settled_requests': yacht_usage['settled_requests'] + wine_usage['settled_requests'],
        'unknown_requests': yacht_usage['unknown_requests'] + wine_usage['unknown_requests'],
        'settled_tokens': yacht_usage['settled_tokens'] + wine_usage['settled_tokens'],
        'existing_reservation_occupancy': yacht_usage['reservation_occupancy'] + wine_usage['reservation_occupancy'],
        'evaluations': yacht_usage['evaluations'] + wine_usage['evaluations'],
        'active_seconds': yacht_usage['active_seconds'] + wine_usage['active_seconds'],
        'yacht': yacht_usage, 'wine': wine_usage}
    new['accounting_units'] = new['settled_tokens'] + new['existing_reservation_occupancy']
    result = {'requests': PRE_COMPAT3_HISTORICAL['requests'] + new['requests'],
        'settled_requests': PRE_COMPAT3_HISTORICAL['settled_requests'] + new['settled_requests'],
        'unknown_requests': PRE_COMPAT3_HISTORICAL['unknown_requests'] + new['unknown_requests'],
        'settled_tokens': PRE_COMPAT3_HISTORICAL['settled_tokens'] + new['settled_tokens'],
        'existing_reservation_occupancy': PRE_COMPAT3_HISTORICAL['existing_reservation_occupancy'] +
            new['existing_reservation_occupancy'],
        'accounting_units': PRE_COMPAT3_HISTORICAL['accounting_units'] + new['accounting_units'],
        'evaluations': PRE_COMPAT3_HISTORICAL['evaluations'] + new['evaluations'],
        'active_seconds': PRE_COMPAT3_HISTORICAL['active_seconds'] + new['active_seconds'],
        'actual_total_tokens': None, 'actual_unknown_tokens': None, 'actual_total_usd': None,
        'reservation_note': PRE_COMPAT3_HISTORICAL['reservation_note'], 'compat3_usage': new}
    if result['unknown_requests'] != 2 or new['wine']['unknown_requests'] != 1:
        raise RuntimeError('compat4 must retain exactly the historical and compat3 unknown requests')
    return result


HISTORICAL_USAGE = historical_usage()
NEW_LIMITS = {key: JOINT_LIMITS[key] - HISTORICAL_USAGE[key]
              for key in ('requests', 'accounting_units', 'evaluations', 'active_seconds')}
TEST_CAMPAIGNS = deepcopy(previous.CONTINUATION_CAMPAIGNS[2:])
if [campaign['name'] for campaign in TEST_CAMPAIGNS] != TEST_ORDER:
    raise RuntimeError('compat4 test-arm coordinates changed')
for campaign in TEST_CAMPAIGNS:
    if campaign['name'].endswith('-frozen'):
        campaign.pop('policy_from', None)
        campaign['fallback_policy_origin'] = 'fixed_reference_no_promotion_after_holdout_unknown'


def _pin(path):
    return {'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}


def parent_evidence():
    paths = {
        'protocol': PARENT_STUDY / 'protocol.json',
        'source_manifest': PARENT_STUDY / 'source-pins.json',
        'STOP': PARENT_STUDY / 'STOP.json',
        'progress': ROOT / 'ds1-compat3-progress.json',
        'yacht_manifest': PARENT_STUDY / 'c-yacht-train/campaign.json',
        'yacht_journal': PARENT_STUDY / 'c-yacht-train/journal.jsonl',
        'yacht_calls': PARENT_STUDY / 'c-yacht-train/calls.jsonl',
        'yacht_result': PARENT_STUDY / 'c-yacht-train/rounds/0001/result.json',
        'wine_manifest': PARENT_STUDY / 'c-wine-val/campaign.json',
        'wine_journal': PARENT_STUDY / 'c-wine-val/journal.jsonl',
        'wine_calls': PARENT_STUDY / 'c-wine-val/calls.jsonl',
        'wine_result': PARENT_STUDY / 'c-wine-val/rounds/0000/result.json',
        'wine_command': PARENT_STUDY / 'command-evidence/run-c-wine-val-round-0.json',
    }
    return {'protocol_sha256': PARENT_PROTOCOL_SHA256,
            'source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'pins': [{'id': key, **_pin(path)} for key, path in paths.items()]}


def verify_parent():
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    stop = json.loads((PARENT_STUDY / 'STOP.json').read_text(encoding='utf-8'))
    if (file_sha(PARENT_STUDY / 'protocol.json') != PARENT_PROTOCOL_SHA256 or
            file_sha(PARENT_STUDY / 'source-pins.json') != PARENT_SOURCE_MANIFEST_SHA256 or
            protocol.get('version') != 'rsi-study-v4-compat3' or
            stop.get('study') != 'rsi-study-v4-compat3' or stop.get('error_type') != 'RuntimeError'):
        raise RuntimeError('compat3 parent protocol/STOP changed')
    yacht, yacht_usage = _campaign_usage('c-yacht-train')
    wine, wine_usage = _campaign_usage('c-wine-val')
    yacht_rounds = [row for row in yacht.events() if row['kind'] == 'round_committed']
    wine_rounds = [row for row in wine.events() if row['kind'] == 'round_committed']
    wine_result = json.loads((PARENT_STUDY / 'c-wine-val/rounds/0000/result.json').read_text(encoding='utf-8'))
    if (yacht_usage['requests'] != 12 or yacht_usage['settled_tokens'] != 128344 or
            yacht_usage['unknown_requests'] != 0 or yacht_usage['evaluations'] != 3 or
            [row['payload']['round'] for row in yacht_rounds] != [1]):
        raise RuntimeError('compat3 Yacht completion evidence changed')
    if (wine_usage['requests'] != 10 or wine_usage['settled_requests'] != 9 or
            wine_usage['unknown_requests'] != 1 or wine_usage['settled_tokens'] != 112662 or
            wine_usage['reservation_occupancy'] != 27818 or wine_usage['evaluations'] != 3 or
            wine_rounds or wine_result.get('status') != 'invalid' or
            wine_result.get('error_type') != 'TimeoutError' or
            wine_result.get('budget', {}).get('blocked') is not True):
        raise RuntimeError('compat3 Wine provider-unknown boundary changed')
    return {'verified': True, 'compat3_stop_preserved': True,
        'yacht_round1_committed': True, 'wine_round_uncommitted_unknown': True,
        'wine_baseline_rmse': wine_result['baseline']['rmse'],
        'wine_baseline_utility': wine_result['baseline']['score'],
        'wine_completed_candidate_scores': [attempt['visible']['score'] for attempt in wine_result['attempts']
                                            if attempt.get('visible')],
        'unknown_request': 10, 'unknown_reservation_tokens': 27818,
        'no_model_replay': True, 'historical_usage': HISTORICAL_USAGE}


def budget_plan():
    ceilings = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for campaign in TEST_CAMPAIGNS:
        for out, key in (('requests', 'max_requests'), ('accounting_units', 'max_tokens'),
                         ('evaluations', 'max_evaluations'), ('active_seconds', 'max_seconds')):
            ceilings[out] += campaign['limits'][key]
    combined = {key: historical_usage()[key] + ceilings[key] for key in ceilings}
    if any(ceilings[key] > NEW_LIMITS[key] or combined[key] > JOINT_LIMITS[key] for key in ceilings):
        raise RuntimeError('compat4 test subset exceeds remaining joint budget')
    return {'joint_limits': JOINT_LIMITS, 'historical': HISTORICAL_USAGE, 'new_limits': NEW_LIMITS,
        'new_campaign_ceilings': ceilings, 'combined_ceiling': combined,
        'unused_unassigned_new_budget': {key: NEW_LIMITS[key] - ceilings[key] for key in ceilings},
        'scope': 'eight same-budget test/final arms; no Wine rerun and no optimizer invocation',
        'comparison_validity': 'fixed/random/greedy comparisons remain direct; trained-frozen is an explicit fixed fallback, so training gain is unidentifiable'}


def assert_joint_budget(budgets):
    new = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for budget in budgets:
        if budget['blocked'] or not budget['usage_complete'] or budget['held_tokens']:
            raise RuntimeError('new unknown/inflight usage permanently stops compat4')
        for out, key in (('requests', 'requests'), ('accounting_units', 'used_tokens'),
                         ('evaluations', 'evaluations'), ('active_seconds', 'wall_seconds')):
            new[out] += budget[key]
    history = historical_usage()
    combined = {key: history[key] + new[key] for key in new}
    if any(new[key] > NEW_LIMITS[key] or combined[key] > JOINT_LIMITS[key] for key in new):
        raise RuntimeError('compat4 joint budget ceiling exceeded')
    return {'historical': history, 'new': new,
        'combined': {**combined, 'actual_total_tokens': None, 'actual_total_usd': None},
        'remaining_authorized': {key: NEW_LIMITS[key] - new[key] for key in new}}


def _rewrite_path(value):
    if isinstance(value, str) and value.startswith(str(PARENT_STUDY)):
        return str(STUDY) + value[len(str(PARENT_STUDY)):]
    if isinstance(value, list):
        return [_rewrite_path(item) for item in value]
    return value


def prepare_argv(campaign):
    result = [_rewrite_path(value) for value in previous.prepare_argv(campaign)]
    # The source study reuses one task path across arms, while ResearchTask.prepare
    # deliberately requires a new root. Distinct roots preserve the same original
    # CSV hash, seed, features, targets and row partitions.
    index = result.index('--task-root')
    result[index + 1] = str(STUDY / 'tasks' / campaign['name'])
    return result


def run_argv(name):
    return [_rewrite_path(value) for value in previous.run_argv(name)]


def final_argv(name):
    return [_rewrite_path(value) for value in previous.final_argv(name)]


def optimizer_decision():
    verification = verify_parent()
    return {'version': 1, 'optimizer_invoked': False, 'promoted': False,
        'selected_parameters': {'max_width': 2, 'max_refine': 1},
        'reason': 'compat3 Wine round terminated on provider-unknown usage; no complete holdout trace or optimizer eligibility',
        'wine_baseline_rmse': verification['wine_baseline_rmse'],
        'wine_completed_candidate_scores': verification['wine_completed_candidate_scores'],
        'trained_frozen_execution': 'same fixed reference parameters; learning contribution unidentifiable',
        'frozen_before_any_compat4_test_result': True, 'no_test_feedback_to_selection': True}


def build_protocol(source_files, *, cli_command, base_url):
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    evidence = parent_evidence()
    verification = verify_parent()
    source_pins = [{'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}
                   for path in source_files]
    return {**protocol, 'version': 'rsi-study-v4-compat4', 'source_pins': source_pins,
        'compatibility_parent_protocol_sha256': PARENT_PROTOCOL_SHA256,
        'compatibility_parent_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
        'compatibility_parent_evidence': evidence,
        'compatibility_amendment': {'previous_protocol_sha256': PARENT_PROTOCOL_SHA256,
            'previous_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'new_model_requests_at_amendment': 0,
            'scope': 'test-only minimal subset after provider-unknown Wine STOP'},
        'historical_verification': verification, 'budget_plan': budget_plan(),
        'optimizer_decision': optimizer_decision(), 'campaigns_new': TEST_CAMPAIGNS,
        'observation': {**protocol['observation'], 'readback_timeout_seconds': 180,
            'max_readonly_queries_per_gate': 8, 'readback_backoff_initial_seconds': 2,
            'readback_backoff_max_seconds': 15, 'provider_retries': 0},
        'new_vs_parent': [*protocol['new_vs_parent'],
            'compat3 provider-unknown Wine request/reservation and all known usage permanently retained',
            'no Wine model request or attempt identity is replayed',
            'no optimizer is run; trained-frozen arms use an explicit fixed-reference fallback',
            'all eight test arms retain their original task, coordinates, seeds and per-arm ceilings',
            'results support direct fixed/random/greedy comparison and do not claim learning gain'],
        'old_v3_v4_compat1_compat2_compat3_permanently_preserved': True}
