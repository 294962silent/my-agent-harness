"""Frozen v4 continuation design; old v3 remains permanently stopped."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from experiments.research_validation import rsi_study as previous
from my_coding_agent.research_continuation import _ArchivedCampaign, verify_import_spec
from my_coding_agent.research_runtime import ResearchBudget
from my_coding_agent.rsi_branch import file_sha

WORKTREE, ROOT = previous.WORKTREE, previous.ROOT
OLD_STUDY, STUDY = ROOT / 'rsi-study-v3', ROOT / 'rsi-study-v4'
JOINT_LIMITS = {'requests': 312, 'accounting_units': 5200000, 'evaluations': 63, 'active_seconds': 15600}
HISTORICAL_USAGE = {'requests': 33, 'settled_requests': 32, 'unknown_requests': 1,
    'settled_tokens': 369696, 'existing_reservation_occupancy': 27762,
    'evaluations': 11, 'active_seconds': 2272.4298025000608, 'actual_total_tokens': None,
    'actual_unknown_tokens': None, 'actual_total_usd': None,
    'reservation_note': 'existing reservation / conservative budget accounting occupancy; not a proved billing-token upper bound'}
NEW_LIMITS = {'requests': 279, 'accounting_units': 4802542, 'evaluations': 52,
              'active_seconds': 13327.57019749994}
OLD_ANCHORS = {
    'protocol.json': '9e6e7609ac23155368fa5b546d71d64a11f2fafd82d1a2312435d418e7786ff8',
    'source-pins.json': 'a6356ba0bcfcf10519d10f620d0d19e52cde03d09e2916b21f5d36edfee03bc6',
    'c-airfoil-train/campaign.json': 'c39a6f48c6d55d208e71ef120db290b3112ecb9ddd29b919c465d000c494e214',
    'c-airfoil-train/journal.jsonl': 'e61ff10e6ef350f2c7415e17020e14107baae3dc8d0b0dfc64dff493abd79dc8',
    'c-yacht-train/campaign.json': '4e8a44701ed9c3b3bc99298993b3e5cdd0ff276b0a291d39695ad02d0659a0d7',
    'c-yacht-train/journal.jsonl': '0a57ae6f8cb7b54d11f576038951dfabaaac97d2a8fa186a32c20824f18beb4f',
}
CONTINUATION_CAMPAIGNS = [deepcopy(c) for c in previous.CAMPAIGNS[1:]]
CONTINUATION_CAMPAIGNS[0]['limits']['max_evaluations'] = 8
CONTINUATION_CAMPAIGNS[1]['limits']['max_evaluations'] = 4
TRAIN_NAMES = ('c-airfoil-train', 'c-yacht-train', 'c-wine-val')
TEST_ORDER = list(previous.TEST_ORDER)


def budget_plan():
    ceilings = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for campaign in CONTINUATION_CAMPAIGNS:
        for out, key in (('requests', 'max_requests'), ('accounting_units', 'max_tokens'),
                         ('evaluations', 'max_evaluations'), ('active_seconds', 'max_seconds')):
            ceilings[out] += campaign['limits'][key]
    old = {'requests': HISTORICAL_USAGE['requests'],
           'accounting_units': HISTORICAL_USAGE['settled_tokens'] + HISTORICAL_USAGE['existing_reservation_occupancy'],
           'evaluations': HISTORICAL_USAGE['evaluations'], 'active_seconds': HISTORICAL_USAGE['active_seconds']}
    combined = {k: old[k] + ceilings[k] for k in old}
    if any(ceilings[k] > NEW_LIMITS[k] or combined[k] > JOINT_LIMITS[k] for k in old):
        raise RuntimeError('planned campaign ceilings exceed authorized joint budget')
    return {'joint_limits': JOINT_LIMITS, 'new_limits': NEW_LIMITS, 'historical': HISTORICAL_USAGE,
            'new_campaign_ceilings': ceilings, 'combined_ceiling': combined,
            'unused_unassigned_new_budget': {k: NEW_LIMITS[k] - ceilings[k] for k in ceilings},
            'accounting_unit_definition': 'new settled provider total_tokens + held reservations; historical known tokens + existing reservation occupancy; actual historical total unavailable',
            'optimizer_cost': 'ratio_cost uses evaluation-count proxy only; no equal actual token-efficiency claim',
            'test_limits': 'all eight test arms retain identical 24 requests / 400000 accounting units /1200 active sec/5 host evaluations ceilings'}


def assert_joint_budget(budgets):
    new = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for b in budgets:
        if b['blocked'] or not b['usage_complete'] or b['held_tokens']:
            raise RuntimeError('new unknown/inflight usage permanently stops v4')
        for out, key in (('requests', 'requests'), ('accounting_units', 'used_tokens'),
                         ('evaluations', 'evaluations'), ('active_seconds', 'wall_seconds')):
            new[out] += b[key]
    old_units = HISTORICAL_USAGE['settled_tokens'] + HISTORICAL_USAGE['existing_reservation_occupancy']
    combined = {'requests': HISTORICAL_USAGE['requests'] + new['requests'],
                'accounting_units': old_units + new['accounting_units'],
                'evaluations': HISTORICAL_USAGE['evaluations'] + new['evaluations'],
                'active_seconds': HISTORICAL_USAGE['active_seconds'] + new['active_seconds'],
                'actual_total_tokens': None, 'actual_total_usd': None}
    if any(new[k] > NEW_LIMITS[k] or combined[k] > JOINT_LIMITS[k] for k in new):
        raise RuntimeError('joint budget ceiling exceeded')
    return {'historical': HISTORICAL_USAGE, 'new': new, 'combined': combined,
            'remaining_authorized': {k: NEW_LIMITS[k] - new[k] for k in new}}


def airfoil_import_spec():
    rows = [json.loads(line) for line in (OLD_STUDY / 'c-airfoil-train/journal.jsonl').read_text(encoding='utf-8').splitlines()]
    manifest = json.loads((OLD_STUDY / 'c-airfoil-train/campaign.json').read_text(encoding='utf-8'))
    return {'version': 1, 'source_root': str(OLD_STUDY / 'c-airfoil-train'),
        'source_archive': str(OLD_STUDY / 'source-frozen'), 'source_pins': str(OLD_STUDY / 'source-pins.json'),
        'manifest_sha256': OLD_ANCHORS['c-airfoil-train/campaign.json'],
        'journal_sha256': OLD_ANCHORS['c-airfoil-train/journal.jsonl'],
        'source_pins_sha256': OLD_ANCHORS['source-pins.json'], 'campaign_id': manifest['campaign_id'],
        'round_events': [row['sha256'] for row in rows if row['kind'] == 'round_committed']}


def verify_historical_accounting():
    for path, expected in OLD_ANCHORS.items():
        if file_sha(OLD_STUDY / path) != expected:
            raise RuntimeError('old v3 immutable evidence changed: ' + path)
    airfoil, _ = verify_import_spec(airfoil_import_spec())
    manifest = json.loads((OLD_STUDY / 'c-yacht-train/campaign.json').read_text(encoding='utf-8'))
    yacht = _ArchivedCampaign(OLD_STUDY / 'c-yacht-train', manifest)
    yacht.source_archive = OLD_STUDY / 'source-frozen'
    yacht.verify()
    states = [ResearchBudget(c) for c in (airfoil, yacht)]
    records = [r for b in states for r in b.records]
    settled = [r for r in records if r['status'] == 'settled']
    unknown = [r for r in records if r['status'] != 'settled']
    active = sum([e['payload']['budget_state']['active_seconds'] for e in b.campaign.events()
                  if 'budget_state' in e['payload']][-1] for b in states)
    measured = {'requests': len(records), 'settled_requests': len(settled), 'unknown_requests': len(unknown),
        'settled_tokens': sum(r['used_tokens'] for r in settled),
        'existing_reservation_occupancy': sum(r['reserved_tokens'] for r in unknown),
        'evaluations': sum(len(b.evaluations) for b in states), 'active_seconds': active}
    if any(abs(measured[k] - HISTORICAL_USAGE[k]) > 1e-8 for k in measured):
        raise RuntimeError('historical usage differs from approved carried occupancy')
    if unknown[0].get('error_type') != 'APIConnectionError' or unknown[0]['usage'] is not None:
        raise RuntimeError('historical unknown marker changed')
    return {'verified': True, 'measured': measured, 'unknown_actual_usage': None,
            'unknown_error_type': unknown[0]['error_type'], 'old_journals_unchanged': True,
            'yacht_rounds_imported': 0, 'airfoil_rounds_imported': 2,
            'old_production_base': '0b63386 plus original per-file source-frozen pins; current HEAD is not old experiment provenance'}


def prepare_argv(campaign):
    argv = previous.prepare_argv(campaign)
    old_prefix = str(previous.STUDY)
    argv = [str(STUDY) + arg[len(old_prefix):] if isinstance(arg, str) and arg.startswith(old_prefix) else arg for arg in argv]
    return argv + ['--observation-contract', 'langfuse_campaign_v1', '--observation-protocol', str(STUDY / 'protocol.json')]


def run_argv(name):
    return ['uv', 'run', '--no-sync', 'python', '-m', 'my_coding_agent.research_cli', 'run',
            str(STUDY / name), '--auth-provider', 'openai', '--auth-profile', 'default']


def final_argv(name):
    return ['uv', 'run', '--no-sync', 'python', '-m', 'my_coding_agent.research_cli', 'final', str(STUDY / name)]


def build_protocol(source_files, *, cli_command, base_url):
    historical = verify_historical_accounting()
    source_pins = [{'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}
                   for path in source_files]
    return {'version': 'rsi-study-v4-continuation', 'frozen_before_new_model_calls': True,
        'historical_airfoil_results_seen': True, 'fresh_full_preregistration': False,
        'old_study_permanently_stopped': True, 'historical_anchors': OLD_ANCHORS,
        'historical_verification': historical, 'history_import': airfoil_import_spec(),
        'budget_plan': budget_plan(), 'scientific_parent_protocol_sha256': OLD_ANCHORS['protocol.json'],
        'scientific_parent_internal_digest': '0730a2a30070e02345673ef6f3c6d3fe706465bd1cf3b09b919e5ae5297e375b',
        'scientific_conditions': json.loads((OLD_STUDY / 'protocol.json').read_text(encoding='utf-8')),
        'campaigns_new': CONTINUATION_CAMPAIGNS, 'test_execution_order': TEST_ORDER,
        'source_pins': source_pins,
        'new_vs_parent': ['explicit read-only Airfoil committed-round import',
            'all old requests/evaluations/active time carried; unknown occupancy permanent, actual usage unavailable',
            'new attempt/campaign identities; uncommitted Yacht round never imported or resent',
            'mandatory raw Langfuse owner and first-two-ordinary-call metadata readback; later real attempt verified',
            'remove unused train/holdout final-evaluation slots to respect remaining 52 evaluations'],
        'observation': {'contract': 'langfuse_campaign_v1', 'sdk_version': '5.2.0', 'base_url': base_url,
            'cli_command': list(cli_command), 'cli_pins': {str(Path(p)): file_sha(Path(p)) for p in cli_command},
            'readback_timeout_seconds': 30, 'max_readonly_queries_per_gate': 3,
            'gate_rule': 'two distinct call_id and observation_id in the same campaign/session and stable-root trace before third dispatch, even in the same attempt; first later real attempt call checked before next dispatch/boundary; no extra paid probe',
            'health': 'required=True raw attach; flush and check after settled usage before next reservation and at close',
            'metadata': 'campaign/session arm stage task_id + parent_id JSON(round,branch,attempt,stage); IO never requested',
            'failure': 'preserve settled usage and STOP; never retry a model call or retrospectively upload v3'},
        'reporting': {'raw_rmse': 'per dataset and own mean-baseline ratio; no cross-target-scale RMSE mean',
            'costs': 'actual settled tokens/requests/evaluations/active seconds plus historical unavailable usage disclosed',
            'learning': 'strict independent Wine promotion only; equivalent frozen/reference policy means learning contribution unidentifiable',
            'final': 'once after frozen policy; no return to tuning or memory'}}
