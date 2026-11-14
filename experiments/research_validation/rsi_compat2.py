"""Second compatibility continuation after the prepaid compat1 driver STOP."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from experiments.research_validation import rsi_compat as parent
from experiments.research_validation import rsi_continuation as scientific_parent
from my_coding_agent.research_runtime import ResearchCampaign
from my_coding_agent.rsi_branch import file_sha

ROOT, WORKTREE = parent.ROOT, parent.WORKTREE
PARENT_STUDY = parent.STUDY
STUDY = ROOT / 'rsi-study-v4-compat2'
PARENT_PROTOCOL_SHA256 = '0c338e6e3147195ea797e62160c42642149587a368be4b5fadad0438f4c630f3'
PARENT_SOURCE_MANIFEST_SHA256 = (
    '99f8a05239bd84dc38e0ca5b2c38677cb9ca155e0a92b3861fa229395dd5a6a8'
)

# The parent study owns the scientific plan. These aliases preserve it exactly and
# make every symbol consumed by the executor explicit in this module.
JOINT_LIMITS = deepcopy(parent.JOINT_LIMITS)
HISTORICAL_USAGE = deepcopy(parent.HISTORICAL_USAGE)
NEW_LIMITS = deepcopy(parent.NEW_LIMITS)
CONTINUATION_CAMPAIGNS = deepcopy(parent.CONTINUATION_CAMPAIGNS)
TRAIN_NAMES = list(scientific_parent.TRAIN_NAMES)
TEST_ORDER = list(parent.TEST_ORDER)
OLD_ANCHORS = deepcopy(parent.OLD_ANCHORS)


def _pin(path):
    return {'path': str(path), 'sha256': file_sha(path), 'bytes': path.stat().st_size}


def parent_evidence():
    receipt_root = ROOT
    paths = [
        PARENT_STUDY / 'protocol.json',
        PARENT_STUDY / 'source-pins.json',
        PARENT_STUDY / 'STOP.json',
        receipt_root / 'ds1-compat-driver-startup-stop.json',
        receipt_root / 'ds1-compat-transport-ready.json',
        receipt_root / 'ds1-compat-transport-real-readback.json',
    ]
    return {'pins': [_pin(path) for path in paths], 'protocol_sha256': PARENT_PROTOCOL_SHA256,
            'source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'new_paid_requests_in_parent': 0, 'new_evaluations_in_parent': 0}


def verify_parent():
    evidence = parent_evidence()
    for pin in evidence['pins']:
        path = Path(pin['path'])
        if file_sha(path) != pin['sha256'] or path.stat().st_size != pin['bytes']:
            raise RuntimeError('compat1 historical evidence changed: ' + path.name)
    protocol = json.loads((PARENT_STUDY / 'protocol.json').read_text(encoding='utf-8'))
    stop = json.loads((PARENT_STUDY / 'STOP.json').read_text(encoding='utf-8'))
    startup = json.loads(Path(parent_evidence()['pins'][3]['path']).read_text(encoding='utf-8'))
    if (file_sha(PARENT_STUDY / 'protocol.json') != PARENT_PROTOCOL_SHA256 or
            file_sha(PARENT_STUDY / 'source-pins.json') != PARENT_SOURCE_MANIFEST_SHA256 or
            protocol.get('version') != 'rsi-study-v4-compat1' or
            stop.get('study') != 'rsi-study-v4-compat1' or stop.get('status') != 'STOP' or
            startup.get('new_paid_requests') != 0 or startup.get('new_evaluations') != 0):
        raise RuntimeError('compat1 parent boundary changed')
    usage = {}
    for name in TRAIN_NAMES:
        state = ResearchCampaign.load(PARENT_STUDY / name).status()
        budget = state['budget']
        usage[name] = {'requests': budget['requests'], 'used_tokens': budget['used_tokens'],
            'evaluations': budget['evaluations'], 'wall_seconds': budget['wall_seconds'],
            'held_tokens': budget['held_tokens']}
    if any(value['requests'] or value['used_tokens'] or value['evaluations'] or value['held_tokens'] or
           value['wall_seconds'] >= 1
           for value in usage.values()):
        raise RuntimeError('compat1 produced paid usage before driver STOP: ' + json.dumps(usage,
            sort_keys=True, separators=(',', ':')))
    parent.verify_historical_accounting()
    return {'verified': True, 'parent_stop_preserved': True, 'parent_runtime_usage': usage,
            'parent_paid_requests': 0, 'parent_paid_evaluations': 0,
            'parent_setup_wall_seconds_below_one': True,
            'parent_protocol_sha256': PARENT_PROTOCOL_SHA256,
            'parent_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256}


def budget_plan():
    plan = deepcopy(parent.budget_plan())
    if plan['new_limits'] != NEW_LIMITS or plan['new_campaign_ceilings']['evaluations'] != 51:
        raise RuntimeError('compat2 budget differs from parent')
    return {**plan, 'parent_runtime_usage': 'zero; compat1 stopped before its first paid call',
            'compat2_ceiling_note': 'parent balance reused unchanged; no budget expansion'}


def assert_joint_budget(budgets):
    return parent.assert_joint_budget(budgets)


def prepare_argv(campaign):
    prefix = str(PARENT_STUDY)
    return [str(STUDY) + arg[len(prefix):]
            if isinstance(arg, str) and arg.startswith(prefix) else arg
            for arg in parent.prepare_argv(campaign)]


def run_argv(name):
    prefix = str(PARENT_STUDY)
    return [str(STUDY) + arg[len(prefix):]
            if isinstance(arg, str) and arg.startswith(prefix) else arg
            for arg in parent.run_argv(name)]


def final_argv(name):
    prefix = str(PARENT_STUDY)
    return [str(STUDY) + arg[len(prefix):]
            if isinstance(arg, str) and arg.startswith(prefix) else arg
            for arg in parent.final_argv(name)]


def build_protocol(source_files, *, cli_command, base_url):
    protocol = parent.build_protocol(source_files, cli_command=cli_command, base_url=base_url)
    evidence = parent_evidence()
    return {**protocol, 'version': 'rsi-study-v4-compat2',
        'compatibility_parent_protocol_sha256': PARENT_PROTOCOL_SHA256,
        'compatibility_parent_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
        'compatibility_parent_evidence': evidence,
        'compatibility_amendment': {
            'previous_protocol_sha256': PARENT_PROTOCOL_SHA256,
            'previous_source_manifest_sha256': PARENT_SOURCE_MANIFEST_SHA256,
            'new_model_requests_at_amendment': 0,
            'scope': 'export TRAIN_NAMES and add an offline driver-path smoke; no scientific change',
            'evidence': str(parent_evidence()['pins'][3]['path'])},
        'historical_verification': verify_parent(), 'budget_plan': budget_plan(),
        'campaigns_new': CONTINUATION_CAMPAIGNS,
        'new_vs_parent': [*protocol['new_vs_parent'],
            'compat1 STOP, zero-usage driver receipt, transport evidence and protocol remain immutable',
            'export TRAIN_NAMES and verify all run/final command entry points offline before payment',
            'fresh compat2 study/campaign identities; no import of compat1 prepared campaigns'],
        'old_v3_v4_compat1_permanently_preserved': True}
