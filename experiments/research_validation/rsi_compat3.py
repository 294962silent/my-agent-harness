"""Third compatibility continuation for settled compat2 usage and one committed round."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from experiments.research_validation import rsi_compat2 as previous
from experiments.research_validation import rsi_continuation as scientific_parent
from my_coding_agent.research_continuation import _ArchivedCampaign, _budget_usage
from my_coding_agent.rsi_branch import file_sha

ROOT, WORKTREE = previous.ROOT, previous.WORKTREE
PARENT_STUDY = previous.STUDY
STUDY = ROOT / 'rsi-study-v4-compat3'
PARENT_PROTOCOL_SHA256 = 'ab86e9b435f40f3a69d2b3a89fdc4eb15bab7119bdd7ad88fa64a3f85ee33b89'
PARENT_SOURCE_MANIFEST_SHA256 = (
    '6511db5ee43bb569301654664ac6851b76178dba9278ef2eeb2d93bd4d3f65c1'
)
PARENT_CAMPAIGN = PARENT_STUDY / 'c-yacht-train'
PARENT_STOP_RECEIPT = ROOT / 'ds1-compat2-stop-receipt.json'
PARENT_FINAL_READBACK = ROOT / 'ds1-compat2-final-readback.json'
COMPAT2_EXPECTED = {'requests': 14, 'settled_tokens': 133196, 'evaluations': 4,
                    'active_seconds_ceiling_delta': .01}

JOINT_LIMITS = deepcopy(previous.JOINT_LIMITS)
PRE_COMPAT2_HISTORICAL = deepcopy(previous.HISTORICAL_USAGE)
CONTINUATION_CAMPAIGNS = deepcopy(previous.CONTINUATION_CAMPAIGNS)
TRAIN_NAMES = list(scientific_parent.TRAIN_NAMES)
TEST_ORDER = list(previous.TEST_ORDER)
OLD_ANCHORS = deepcopy(previous.OLD_ANCHORS)


def _source_usage():
    manifest = json.loads((PARENT_CAMPAIGN / 'campaign.json').read_text(encoding='utf-8'))
    old = _ArchivedCampaign(PARENT_CAMPAIGN, manifest)
    old.source_archive = PARENT_STUDY / 'source-frozen'
    old.source_file_map = {}
    pins = json.loads((PARENT_STUDY / 'source-pins.json').read_text(encoding='utf-8'))
    for index, pin in enumerate(pins):
        name = Path(pin['path']).name
        artifact = old.source_archive / f'{index:02d}-{name}'
        if file_sha(artifact) != pin['sha256'] or artifact.stat().st_size != pin['bytes']:
            raise RuntimeError('compat2 frozen source archive changed: ' + name)
        if name in manifest['implementation'] and pin['sha256'] == manifest['implementation'][name]:
            old.source_file_map[name] = artifact
    return old, _budget_usage(old)


def historical_usage():
    _, source = _source_usage()
    accounting = source['settled_tokens'] + source['reservation_occupancy']
    return {'requests': PRE_COMPAT2_HISTORICAL['requests'] + source['requests'],
        'settled_requests': PRE_COMPAT2_HISTORICAL['settled_requests'] + source['settled_requests'],
        'unknown_requests': PRE_COMPAT2_HISTORICAL['unknown_requests'] + source['unknown_requests'],
        'settled_tokens': PRE_COMPAT2_HISTORICAL['settled_tokens'] + source['settled_tokens'],
        'existing_reservation_occupancy': PRE_COMPAT2_HISTORICAL['existing_reservation_occupancy'] +
            source['reservation_occupancy'],
        'accounting_units': PRE_COMPAT2_HISTORICAL['settled_tokens'] +
            PRE_COMPAT2_HISTORICAL['existing_reservation_occupancy'] + accounting,
        'evaluations': PRE_COMPAT2_HISTORICAL['evaluations'] + source['evaluations'],
        'active_seconds': PRE_COMPAT2_HISTORICAL['active_seconds'] + source['active_seconds'],
        'actual_total_tokens': None, 'actual_unknown_tokens': None, 'actual_total_usd': None,
        'reservation_note': PRE_COMPAT2_HISTORICAL['reservation_note'],
        'compat2_settled_usage': source}


HISTORICAL_USAGE = historical_usage()
NEW_LIMITS = {key: JOINT_LIMITS[key] - HISTORICAL_USAGE[key]
              for key in ('requests', 'evaluations', 'active_seconds')}
NEW_LIMITS['accounting_units'] = JOINT_LIMITS['accounting_units'] - HISTORICAL_USAGE['accounting_units']

# Sunk compat2 allocation is removed from the new campaign while the full scientific
# round layout remains two rounds. The already graded baseline of round 1 is reused.
CONTINUATION_CAMPAIGNS[0]['limits'] = {
    'max_requests': 48 - HISTORICAL_USAGE['compat2_settled_usage']['requests'],
    'max_tokens': 800000 - HISTORICAL_USAGE['compat2_settled_usage']['settled_tokens'],
    'max_seconds': 2400 - HISTORICAL_USAGE['compat2_settled_usage']['active_seconds'],
    'max_evaluations': 7 - HISTORICAL_USAGE['compat2_settled_usage']['evaluations'],
    'max_output_tokens': 16384,
}


def _pin(path):
    return {'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}


def parent_evidence():
    paths = {
        'protocol': PARENT_STUDY / 'protocol.json',
        'source_manifest': PARENT_STUDY / 'source-pins.json',
        'STOP': PARENT_STUDY / 'STOP.json',
        'progress': ROOT / 'ds1-compat2-progress.json',
        'stop_receipt': PARENT_STOP_RECEIPT,
        'final_readback': PARENT_FINAL_READBACK,
        'yacht_manifest': PARENT_CAMPAIGN / 'campaign.json',
        'yacht_journal': PARENT_CAMPAIGN / 'journal.jsonl',
        'yacht_calls': PARENT_CAMPAIGN / 'calls.jsonl',
        'round0_report': PARENT_CAMPAIGN / 'rounds/0000/result.json',
        'round0_trace': PARENT_CAMPAIGN / 'rounds/0000/trace.json',
        'round1_report': PARENT_CAMPAIGN / 'rounds/0001/result.json',
        'round0_command': PARENT_STUDY / 'command-evidence/run-c-yacht-train-round-0.json',
        'round1_command': PARENT_STUDY / 'command-evidence/run-c-yacht-train-round-1.json',
    }
    return {'protocol_sha256': PARENT_PROTOCOL_SHA256,
            'source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'pins': [{'id': name, **_pin(path)} for name, path in paths.items()]}


def verify_parent():
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    if (file_sha(PARENT_STUDY / 'protocol.json') != PARENT_PROTOCOL_SHA256 or
            file_sha(PARENT_STUDY / 'source-pins.json') != PARENT_SOURCE_MANIFEST_SHA256 or
            protocol.get('version') != 'rsi-study-v4-compat2'):
        raise RuntimeError('compat2 protocol/source manifest changed')
    receipt = json.loads(PARENT_STOP_RECEIPT.read_text(encoding='utf-8'))
    readback = json.loads(PARENT_FINAL_READBACK.read_text(encoding='utf-8'))
    usage = receipt['budget']['new']
    if (receipt.get('study') != 'rsi-study-v4-compat2' or
            receipt.get('status') != 'STOP_READBACK_TIMEOUT_BEFORE_THIRD_REQUEST' or
            receipt.get('protocol_sha256') != PARENT_PROTOCOL_SHA256 or
            receipt.get('source_manifest_sha256') != PARENT_SOURCE_MANIFEST_SHA256 or
            usage.get('requests') != COMPAT2_EXPECTED['requests'] or
            usage.get('accounting_units') != COMPAT2_EXPECTED['settled_tokens'] or
            usage.get('evaluations') != COMPAT2_EXPECTED['evaluations'] or
            usage.get('unknown_requests') != 0 or usage.get('held_tokens') != 0 or
            usage.get('usage_complete') is not True or
            abs(usage.get('active_seconds', 0) - 1163.9698651999934) > .01):
        raise RuntimeError('compat2 STOP usage receipt changed')
    if (readback.get('all_calls_matched') is not True or readback.get('expected_calls') != 14 or
            readback.get('matched_calls') != 14 or readback.get('distinct_observation_ids') != 14 or
            readback.get('local_usage_total_sum') != 133196 or
            readback.get('remote_usage_total_sum') != 133196 or readback.get('new_model_requests') != 0):
        raise RuntimeError('compat2 late readback evidence changed')
    old, measured = _source_usage()
    old.verify()
    events = old.events()
    committed = [row for row in events if row['kind'] == 'round_committed']
    started = [row['payload']['round'] for row in events if row['kind'] == 'round_started']
    if ([row['payload']['round'] for row in committed] != [0] or started != [0, 1] or
            any(row['kind'] in ('final_started', 'optimizer_started') for row in events)):
        raise RuntimeError('compat2 committed/uncommitted boundary changed')
    if (measured['requests'] != 14 or measured['settled_requests'] != 14 or
            measured['unknown_requests'] != 0 or measured['settled_tokens'] != 133196 or
            measured['reservation_occupancy'] != 0 or measured['evaluations'] != 4 or
            measured['completed_evaluations'] != 4 or
            not (1163.9698651999934 <= measured['active_seconds'] < 1164.1)):
        raise RuntimeError('compat2 settled accounting changed')
    records = _budget_records(old)
    tail_requests = records[12:14]
    tail_evaluations = [item for item in _budget_evaluations(old) if item.get('stage') == 'baseline']
    if ([item['request'] for item in tail_requests] != [13, 14] or
            any(item['status'] != 'settled' for item in tail_requests) or
            [item['evaluation'] for item in tail_evaluations] != [4]):
        raise RuntimeError('compat2 uncommitted round1 sunk tail changed')
    evidence = parent_evidence()
    return {'verified': True, 'stop_preserved': True, 'late_readback_verified': True,
            'source_runtime_usage': measured, 'historical_usage': HISTORICAL_USAGE,
            'committed_rounds_imported': [0], 'uncommitted_rounds_sunk': [1],
            'uncommitted_request_numbers': [item['request'] for item in tail_requests],
            'replayed_request_numbers': [], 'model_call_retries': 0,
            'evidence_sha256': {row['id']: row['sha256'] for row in evidence['pins']},
            'old_v3_v4_compat1_compat2_preserved': True}


def _budget_records(old):
    from my_coding_agent.research_runtime import ResearchBudget
    return ResearchBudget(old).records


def _budget_evaluations(old):
    from my_coding_agent.research_runtime import ResearchBudget
    return ResearchBudget(old).evaluations


def yacht_import_spec():
    old, usage = _source_usage()
    events = old.events()
    manifest = old.manifest
    return {'version': 2, 'mode': 'committed_prefix',
        'source_root': str(PARENT_CAMPAIGN), 'source_archive': str(PARENT_STUDY / 'source-frozen'),
        'source_pins': str(PARENT_STUDY / 'source-pins.json'),
        'manifest_sha256': file_sha(PARENT_CAMPAIGN / 'campaign.json'),
        'journal_sha256': file_sha(PARENT_CAMPAIGN / 'journal.jsonl'),
        'source_pins_sha256': PARENT_SOURCE_MANIFEST_SHA256, 'campaign_id': manifest['campaign_id'],
        'round_events': [row['sha256'] for row in events if row['kind'] == 'round_committed'],
        'uncommitted_rounds': [1], 'source_runtime_usage': usage}


def yacht_baseline_import_spec():
    report_path = PARENT_CAMPAIGN / 'rounds/0001/result.json'
    report = json.loads(report_path.read_text(encoding='utf-8'))
    measured = report['baseline']
    prediction = Path(measured['artifacts']['predictions'])
    measurement = prediction.parent.parent / 'evaluation.json'
    manifest = json.loads((PARENT_CAMPAIGN / 'campaign.json').read_text(encoding='utf-8'))
    paths = [PARENT_CAMPAIGN / 'campaign.json', PARENT_CAMPAIGN / 'journal.jsonl',
        PARENT_CAMPAIGN / 'calls.jsonl', report_path, measurement, prediction,
        PARENT_STUDY / 'protocol.json', PARENT_STUDY / 'source-pins.json', PARENT_STUDY / 'STOP.json',
        PARENT_STOP_RECEIPT, PARENT_FINAL_READBACK]
    return {'version': 1, 'round': 1, 'source_root': str(PARENT_CAMPAIGN),
        'source_campaign_id': manifest['campaign_id'], 'source_pins': str(PARENT_STUDY / 'source-pins.json'),
        'source_archive': str(PARENT_STUDY / 'source-frozen'), 'source_evaluation': 4,
        'report': str(report_path), 'measurement': str(measurement),
        'anchors': {str(path): file_sha(path) for path in paths}}


def budget_plan():
    ceilings = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for campaign in CONTINUATION_CAMPAIGNS:
        for out, key in (('requests', 'max_requests'), ('accounting_units', 'max_tokens'),
                         ('evaluations', 'max_evaluations'), ('active_seconds', 'max_seconds')):
            ceilings[out] += campaign['limits'][key]
    combined = {key: historical_usage()[key] + ceilings[key]
                for key in ('requests', 'accounting_units', 'evaluations', 'active_seconds')}
    if any(ceilings[key] > NEW_LIMITS[key] or combined[key] > JOINT_LIMITS[key] for key in ceilings):
        raise RuntimeError('compat3 campaign ceilings exceed remaining authorized budget')
    return {'joint_limits': JOINT_LIMITS, 'historical': HISTORICAL_USAGE, 'new_limits': NEW_LIMITS,
        'new_campaign_ceilings': ceilings, 'combined_ceiling': combined,
        'unused_unassigned_new_budget': {key: NEW_LIMITS[key] - ceilings[key] for key in ceilings},
        'inheritance': 'compat2 requests/tokens/evaluations/time retained once; round1 sunk requests never replayed',
        'round1_baseline_reuse': 'one completed compat2 host baseline reused; three new candidate evaluations remain',
        'accounting_unit_definition': 'settled provider tokens plus held reservations; no actual unknown usage invented'}


def assert_joint_budget(budgets):
    new = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for budget in budgets:
        if budget['blocked'] or not budget['usage_complete'] or budget['held_tokens']:
            raise RuntimeError('new unknown/inflight usage permanently stops compat3')
        for out, key in (('requests', 'requests'), ('accounting_units', 'used_tokens'),
                         ('evaluations', 'evaluations'), ('active_seconds', 'wall_seconds')):
            new[out] += budget[key]
    history = historical_usage()
    combined = {key: history[key] + new[key]
                for key in ('requests', 'accounting_units', 'evaluations', 'active_seconds')}
    if any(new[key] > NEW_LIMITS[key] or combined[key] > JOINT_LIMITS[key] for key in new):
        raise RuntimeError('compat3 joint budget ceiling exceeded')
    return {'historical': history, 'new': new, 'combined': {**combined,
            'actual_total_tokens': None, 'actual_total_usd': None},
            'remaining_authorized': {key: NEW_LIMITS[key] - new[key] for key in new}}


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
    # Parent compatibility is verified through its frozen journal/source archive. Calling
    # the parent builder would incorrectly require current production sources to equal the
    # already frozen parent pins.
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    evidence = parent_evidence()
    verification = verify_parent()
    source_pins = [{'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}
                   for path in source_files]
    return {**protocol, 'version': 'rsi-study-v4-compat3',
        'source_pins': source_pins,
        'compatibility_parent_protocol_sha256': PARENT_PROTOCOL_SHA256,
        'compatibility_parent_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
        'compatibility_parent_evidence': evidence,
        'compatibility_amendment': {
            'previous_protocol_sha256': PARENT_PROTOCOL_SHA256,
            'previous_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'new_model_requests_at_amendment': 0,
            'scope': 'raise observation-only readback bound; import committed round0; retain sunk round1 cost',
            'model_data_tasks_arms_coordinates_algorithm_unchanged': True},
        'historical_verification': verification, 'budget_plan': budget_plan(),
        'yacht_history_import': yacht_import_spec(),
        'yacht_baseline_imports': {'1': yacht_baseline_import_spec()},
        'campaigns_new': CONTINUATION_CAMPAIGNS,
        'observation': {**protocol['observation'],
            'readback_timeout_seconds': 180, 'max_readonly_queries_per_gate': 8,
            'readback_backoff_initial_seconds': 2, 'readback_backoff_max_seconds': 15,
            'failure': 'bounded observation API retries only; provider unknown or exhausted readback remains fail-closed; no model retry or campaign replay'},
        'new_vs_parent': [*protocol['new_vs_parent'],
            'compat2 STOP and all 14 settled requests/4 completed evaluations/time remain immutable historical cost',
            'only committed Yacht round0 and its evidence are imported; uncommitted round1 requests have new provenance excluded',
            'reuse exact completed Yacht round1 baseline measurement; new attempt/call identities remain distinct',
            'read-only Langfuse metadata queries use at least a 180 second bound with bounded exponential backoff',
            'final/test feedback remains isolated after optimizer policy freeze'],
        'old_v3_v4_compat1_compat2_permanently_preserved': True}
