"""One explicit compatibility continuation; both prior STOP studies stay immutable."""
from __future__ import annotations
from copy import deepcopy
import json
from pathlib import Path

from experiments.research_validation import rsi_continuation as previous
from my_coding_agent.research_continuation import verify_baseline_import
from my_coding_agent.research_runtime import ResearchCampaign
from my_coding_agent.rsi_branch import file_sha

ROOT, WORKTREE, OLD_STUDY = previous.ROOT, previous.WORKTREE, previous.OLD_STUDY
FAILED_STUDY = previous.STUDY
STUDY = ROOT / 'rsi-study-v4-compat1'
JOINT_LIMITS = deepcopy(previous.JOINT_LIMITS)
HISTORICAL_USAGE = {'requests': 35, 'settled_requests': 34, 'unknown_requests': 1,
    'settled_tokens': 372398, 'existing_reservation_occupancy': 27762, 'evaluations': 12,
    'active_seconds': 2290.107044100063, 'actual_total_tokens': None, 'actual_unknown_tokens': None,
    'actual_total_usd': None, 'reservation_note': previous.HISTORICAL_USAGE['reservation_note']}
NEW_LIMITS = {'requests': 277, 'accounting_units': 4799840, 'evaluations': 51,
    'active_seconds': 13309.892955899937}
CONTINUATION_CAMPAIGNS = deepcopy(previous.CONTINUATION_CAMPAIGNS)
CONTINUATION_CAMPAIGNS[0]['limits']['max_evaluations'] = 7
TEST_ORDER = list(previous.TEST_ORDER)
OLD_ANCHORS = deepcopy(previous.OLD_ANCHORS)


def baseline_spec():
    root = FAILED_STUDY / 'c-yacht-train'
    report = root / 'rounds/0000/result.json'
    measured = json.loads(report.read_text(encoding='utf-8'))['baseline']
    prediction = Path(measured['artifacts']['predictions'])
    measurement = prediction.parent.parent / 'evaluation.json'
    manifest = json.loads((root / 'campaign.json').read_text(encoding='utf-8'))
    paths = [root / 'campaign.json', root / 'journal.jsonl', root / 'calls.jsonl', report,
        measurement, prediction, FAILED_STUDY / 'STOP.json', FAILED_STUDY / 'protocol.json',
        FAILED_STUDY / 'source-pins.json']
    return {'version': 1, 'source_root': str(root), 'source_campaign_id': manifest['campaign_id'],
        'source_pins': str(FAILED_STUDY / 'source-pins.json'),
        'source_archive': str(FAILED_STUDY / 'source-frozen'), 'source_evaluation': 1,
        'report': str(report), 'measurement': str(measurement),
        'anchors': {str(path): file_sha(path) for path in paths}}


def verify_historical_accounting():
    old = previous.verify_historical_accounting()
    spec = baseline_spec()
    # Frozen receipt prevents a later mutation from silently becoming a new anchor.
    receipt = ROOT / 'ds1-v4-stop-frozen-anchors.json'
    anchors = json.loads(receipt.read_text(encoding='utf-8'))
    if spec['anchors'] != anchors:
        raise RuntimeError('compat historical anchors changed')
    root = FAILED_STUDY / 'c-yacht-train'
    rows = [json.loads(line) for line in (root / 'journal.jsonl').read_text(encoding='utf-8').splitlines()]
    state = next(row['payload']['budget_state'] for row in reversed(rows) if 'budget_state' in row['payload'])
    if (len(state['records']) != 2 or any(r['status'] != 'settled' for r in state['records']) or
            sum(r['used_tokens'] for r in state['records']) != 2702 or len(state['evaluations']) != 1 or
            abs(state['active_seconds'] - 17.677241600002162) > 1e-7 or
            any(row['kind'] == 'round_committed' for row in rows)):
        raise RuntimeError('v4 sunk accounting changed')
    m = json.loads((root / 'campaign.json').read_text(encoding='utf-8'))
    temporary = ResearchCampaign(root, m)
    temporary.manifest = {**m, 'baseline_import': spec}
    verify_baseline_import(temporary)
    return {**old, 'v4_stopped': True, 'v4_settled_requests': 2, 'v4_settled_tokens': 2702,
        'v4_evaluations': 1, 'v4_active_seconds': state['active_seconds'],
        'v4_committed_rounds_imported': 0, 'baseline_measurements_reused': 1,
        'historical_usage': HISTORICAL_USAGE}


def budget_plan():
    plan = previous.budget_plan()
    ceilings = deepcopy(plan['new_campaign_ceilings'])
    ceilings['evaluations'] = 51
    old = {'requests': 35, 'accounting_units': 400160, 'evaluations': 12,
           'active_seconds': HISTORICAL_USAGE['active_seconds']}
    combined = {k: old[k] + ceilings[k] for k in old}
    if any(ceilings[k] > NEW_LIMITS[k] or combined[k] > JOINT_LIMITS[k] for k in old):
        raise RuntimeError('compat campaign ceilings exceed authorized budget')
    return {**plan, 'new_limits': NEW_LIMITS, 'historical': HISTORICAL_USAGE,
        'new_campaign_ceilings': ceilings, 'combined_ceiling': combined,
        'unused_unassigned_new_budget': {k: NEW_LIMITS[k] - ceilings[k] for k in old},
        'baseline_reuse': 'one unchanged v4 host measurement; old evaluation remains paid/countable once; Yacht7+Wine4+test40=51 new evaluations'}


def assert_joint_budget(budgets):
    new = {'requests': 0, 'accounting_units': 0, 'evaluations': 0, 'active_seconds': 0}
    for b in budgets:
        if b['blocked'] or not b['usage_complete'] or b['held_tokens']:
            raise RuntimeError('new unknown/inflight usage permanently stops compatibility continuation')
        for out, key in (('requests', 'requests'), ('accounting_units', 'used_tokens'),
                         ('evaluations', 'evaluations'), ('active_seconds', 'wall_seconds')):
            new[out] += b[key]
    combined = {'requests': 35 + new['requests'], 'accounting_units': 400160 + new['accounting_units'],
        'evaluations': 12 + new['evaluations'], 'active_seconds': HISTORICAL_USAGE['active_seconds'] + new['active_seconds'],
        'actual_total_tokens': None, 'actual_total_usd': None}
    if any(new[k] > NEW_LIMITS[k] or combined[k] > JOINT_LIMITS[k] for k in new):
        raise RuntimeError('compat joint budget ceiling exceeded')
    return {'historical': HISTORICAL_USAGE, 'new': new, 'combined': combined,
        'remaining_authorized': {k: NEW_LIMITS[k] - new[k] for k in new}}


def prepare_argv(campaign):
    prefix = str(FAILED_STUDY)
    args = [str(STUDY) + a[len(prefix):] if isinstance(a, str) and a.startswith(prefix) else a
            for a in previous.prepare_argv(campaign)]
    if campaign['name'] == 'c-yacht-train':
        args += ['--baseline-import', str(STUDY / 'baseline-import.json')]
    return args


def run_argv(name):
    return ['uv', 'run', '--no-sync', 'python', '-m', 'my_coding_agent.research_cli', 'run',
        str(STUDY / name), '--auth-provider', 'openai', '--auth-profile', 'default']


def final_argv(name):
    return ['uv', 'run', '--no-sync', 'python', '-m', 'my_coding_agent.research_cli', 'final', str(STUDY / name)]


def build_protocol(source_files, *, cli_command, base_url):
    protocol = previous.build_protocol(source_files, cli_command=cli_command, base_url=base_url)
    protocol['observation'] = {**protocol['observation'], 'readback_transport': 'python_urllib_v1',
        'readback_request_timeout_seconds': 20, 'readback_endpoint_path': '/api/public/v2/observations',
        'readback_fields': 'core,basic,model,usage,metadata', 'proxy_policy': 'host urllib system proxy discovery; no global proxy changes'}
    protocol['prepaid_transport_amendment'] = {
        'previous_protocol_sha256': 'f3b48e119989e77eb9e88214139fc839862963501460677db1b68bd811c357db',
        'previous_source_manifest_sha256': 'a080a6389d88fd4ca08c28eb6f25426c1ac5f4d916c6b56e8871ae6a77073c77',
        'archive': str(ROOT / 'rsi-study-v4-compat1-prepaid-cli-archive'), 'new_model_requests_at_amendment': 0,
        'scope': 'read-only transport CLI to urllib; same official endpoint, metadata fields, gate and all scientific conditions',
        'evidence': str(ROOT / 'ds1-compat-python-transport-probe.json')}
    return {**protocol, 'version': 'rsi-study-v4-compat1',
        'compatibility_parent_protocol_sha256': file_sha(FAILED_STUDY / 'protocol.json'),
        'historical_verification': verify_historical_accounting(), 'baseline_import': baseline_spec(),
        'budget_plan': budget_plan(), 'campaigns_new': CONTINUATION_CAMPAIGNS,
        'new_vs_parent': ['strict dict/string parent_id compatibility only',
            'both prior STOP studies unchanged; all costs retained; new campaign and attempt identities',
            'reuse one fully pinned host baseline measurement once, not a new score/evaluation; no model-call replay'],
        'old_v4_permanently_stopped': True}
