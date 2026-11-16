"""Explicit, read-only verification and import of fully committed historical rounds.

This is not request recovery. Historical request records are never settled, copied
into a fresh request ledger, or dispatched again. A study carries all past costs
separately; imported rounds retain the original campaign/source identity.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

from .rsi_branch import file_sha, inventory, plain_path
from .research_runtime import ResearchCampaign, ResearchIntegrityError, atomic_bytes, canonical


class _ArchivedCampaign(ResearchCampaign):
    def implementation_file(self, name):
        return self.source_file_map[name] if hasattr(self, 'source_file_map') else self.source_archive / name

    def append(self, *_args, **_kwargs):
        raise ResearchIntegrityError('historical campaign is read-only')


def _budget_usage(old):
    from .research_runtime import ResearchBudget

    budget = ResearchBudget(old)
    states = [row['payload']['budget_state'] for row in old.events()
              if 'budget_state' in row['payload']]
    return {'requests': len(budget.records), 'settled_requests': sum(
                record['status'] == 'settled' for record in budget.records),
            'unknown_requests': sum(record['status'] != 'settled' for record in budget.records),
            'settled_tokens': sum(record['used_tokens'] or 0 for record in budget.records
                                  if record['status'] == 'settled'),
            'reservation_occupancy': sum(record['reserved_tokens'] for record in budget.records
                                         if record['status'] != 'settled'),
            'evaluations': len(budget.evaluations),
            'completed_evaluations': sum(item['status'] == 'completed' for item in budget.evaluations),
            'active_seconds': states[-1]['active_seconds'] if states else 0}


def verify_import_spec(spec):
    """Read the exact frozen source and journal without relaxing normal load()."""
    from .dream_rsi_contract import validate_trace
    try:
        if type(spec.get('version')) is not int or spec['version'] not in (1, 2):
            raise ValueError('unsupported history import')
        source = plain_path(spec['source_root']).resolve()
        archive = plain_path(spec['source_archive']).resolve()
        pins = plain_path(spec['source_pins']).resolve()
        for path, expected in ((source / 'campaign.json', spec['manifest_sha256']),
                               (source / 'journal.jsonl', spec['journal_sha256']),
                               (pins, spec['source_pins_sha256'])):
            if file_sha(path) != expected:
                raise ValueError('historical anchor changed')
        manifest = json.loads((source / 'campaign.json').read_text(encoding='utf-8'))
        if manifest['campaign_id'] != spec['campaign_id'] or manifest.get('history_import'):
            raise ValueError('historical campaign identity changed or nested import')
        pinned, seen_artifacts, source_file_map = {}, set(), {}
        for index, pin in enumerate(json.loads(pins.read_text(encoding='utf-8'))):
            name = Path(pin['path']).name
            if name in ('.', '..'):
                raise ValueError('invalid frozen source')
            artifact = archive / name
            if not artifact.exists():
                artifact = archive / f'{index:02d}-{name}'
            resolved = artifact.resolve()
            if resolved in seen_artifacts:
                raise ValueError('duplicate frozen source artifact')
            seen_artifacts.add(resolved)
            if file_sha(artifact) != pin['sha256'] or artifact.stat().st_size != pin['bytes']:
                raise ValueError('historical source archive changed')
            if name in manifest['implementation'] and pin['sha256'] == manifest['implementation'][name]:
                pinned[name] = pin['sha256']
                source_file_map[name] = artifact
        if any(pinned.get(name) != sha for name, sha in manifest['implementation'].items()):
            raise ValueError('historical implementation pins disagree')
        old = _ArchivedCampaign(source, manifest)
        old.source_archive = archive
        old.source_file_map = source_file_map
        old.verify()
        events = old.events()
        if any(e['kind'] in ('final_started', 'optimizer_started') for e in events):
            raise ValueError('historical import must precede optimization/final')
        rounds = [e for e in events if e['kind'] == 'round_committed']
        if ([e['sha256'] for e in rounds] != spec['round_events'] or
                [e['payload']['round'] for e in rounds] != list(range(manifest['rounds']))):
            if spec['version'] == 1 or [e['payload']['round'] for e in rounds] != list(
                    range(len(spec['round_events']))):
                raise ValueError('historical round identities changed')
        if manifest['split'] != 'train':
            raise ValueError('only prefinal training campaigns may be imported')
        if spec['version'] == 1:
            if old.ensure_ready() != manifest['rounds'] or len(rounds) != manifest['rounds']:
                raise ValueError('full historical import requires every committed round')
        else:
            if spec.get('mode') != 'committed_prefix' or not rounds or len(rounds) >= manifest['rounds']:
                raise ValueError('prefix import requires a committed prefix and an uncommitted tail')
            started = [e['payload']['round'] for e in events if e['kind'] == 'round_started']
            committed_numbers = [e['payload']['round'] for e in rounds]
            expected_tail = list(range(len(rounds), manifest['rounds']))
            if (committed_numbers != list(range(len(rounds))) or
                    [number for number in started if number not in committed_numbers] != expected_tail or
                    spec.get('uncommitted_rounds') != expected_tail):
                raise ValueError('historical uncommitted tail changed')
            usage = _budget_usage(old)
            if (usage['unknown_requests'] or usage['completed_evaluations'] != usage['evaluations'] or
                    usage != spec.get('source_runtime_usage')):
                raise ValueError('historical settled accounting changed or incomplete')
        for row in rounds:
            p = row['payload']
            report = json.loads((source / p['report']).read_text(encoding='utf-8'))
            trace_path = source / p['trace']
            if file_sha(trace_path) != report['trace_sha256']:
                raise ValueError('historical trace changed')
            trace = json.loads(trace_path.read_text(encoding='utf-8'))
            validate_trace(trace)
            if trace['baseline']['combined_score'] != report['baseline']['score']:
                raise ValueError('historical baseline binding changed')
        return old, rounds
    except ResearchIntegrityError:
        raise
    except (ValueError, OSError, KeyError, TypeError) as exc:
        raise ResearchIntegrityError('historical import evidence changed or incomplete') from exc


def verify_imported_rounds(campaign):
    """Check copied material against immutable old event identities on every load."""
    spec = campaign.manifest['history_import']
    old, rounds = verify_import_spec(spec)
    if (campaign.task.manifest_sha256 != old.task.manifest_sha256 or
            campaign.manifest['baseline_sha256'] != old.manifest['baseline_sha256'] or
            campaign.manifest['rounds'] != old.manifest['rounds'] or
            campaign.manifest['delivery_contract'] != old.manifest.get('delivery_contract', 'end_turn_v1')):
        raise ResearchIntegrityError('import task, baseline, delivery or round ownership changed')
    imported = [r for r in campaign.events() if r['kind'] == 'round_imported']
    if len(imported) != len(rounds):
        raise ResearchIntegrityError('incomplete historical import; execution forbidden')
    for row, origin in zip(imported, rounds, strict=True):
        actual = deepcopy(row['payload'])
        source = actual.pop('history_origin')
        expected = {'source_campaign_id': old.manifest['campaign_id'],
                    'source_manifest_sha256': spec['manifest_sha256'],
                    'source_event_sha256': origin['sha256']}
        if source != expected or actual != origin['payload']:
            raise ResearchIntegrityError('imported round provenance changed')
        for key in ('report', 'trace'):
            if (campaign.root / actual[key]).read_bytes() != (old.root / actual[key]).read_bytes():
                raise ResearchIntegrityError('imported evidence differs from source')
    if spec['version'] == 2:
        completed = [row['payload']['round'] for row in campaign.events()
                     if row['kind'] == 'round_committed']
        if completed and completed != list(range(len(imported), len(imported) + len(completed))):
            raise ResearchIntegrityError('new continuation rounds are not a contiguous committed suffix')
    return old


def _import_verified(root, old, rounds, spec, *, baseline_imports=None, limits=None,
                     observation_contract=None, observation_protocol=None):
    m = old.manifest
    result = ResearchCampaign.prepare(old.task, root, image=m['image'], rounds=m['rounds'],
        arm=m['arm'], split=m['split'], limits=deepcopy(limits or m['limits']), model=deepcopy(m['model']),
        parameters=deepcopy(m['policy']['parameters']), max_attempts=m['max_attempts'],
        attempt_max_turns=m['attempt_max_turns'], context_budget=m['context_budget'],
        schedule=m['schedule'], evaluator_options=deepcopy(m['evaluator_options']),
        memory_source=old.root / 'memory-snapshot', memory_mode=m['memory_mode'],
        mem0_config=deepcopy(m['mem0_config']), replay_budgets=m['optimization']['budgets'],
        delivery_contract=m.get('delivery_contract', 'end_turn_v1'),
        planner_seed=m.get('planner_seed'), history_import=spec,
        baseline_imports=deepcopy(baseline_imports), observation_contract=observation_contract,
        observation_protocol=deepcopy(observation_protocol))
    for row in rounds:
        p = deepcopy(row['payload'])
        for key in ('report', 'trace'):
            atomic_bytes(result.root / p[key], (old.root / p[key]).read_bytes())
        checkpoint = result.root / p['checkpoint']
        atomic_bytes(checkpoint / 'train.py', (old.root / p['checkpoint'] / 'train.py').read_bytes())
        if set(inventory(checkpoint)) != {'train.py'}:
            raise ResearchIntegrityError('unexpected imported files')
        p['history_origin'] = {'source_campaign_id': m['campaign_id'],
            'source_manifest_sha256': spec['manifest_sha256'], 'source_event_sha256': row['sha256']}
        atomic_bytes(checkpoint.parent / f"{p['round']:04d}.json", canonical(p))
        result.append('round_imported', p)
    result.verify()
    return result


def import_completed_campaign(root, spec):
    """Create a distinct immutable campaign anchor; perform zero evaluations/calls."""
    if spec.get('version') != 1:
        raise ResearchIntegrityError('full import requires a version 1 history specification')
    old, rounds = verify_import_spec(spec)  # All checks before creating a destination.
    return _import_verified(root, old, rounds, spec)


def import_committed_campaign_prefix(root, spec, *, baseline_imports=None, limits=None,
                                     observation_contract=None, observation_protocol=None):
    """Import verified committed rounds while retaining a settled uncommitted tail as cost."""
    if spec.get('version') != 2:
        raise ResearchIntegrityError('prefix import requires a version 2 history specification')
    old, rounds = verify_import_spec(spec)
    return _import_verified(root, old, rounds, spec, baseline_imports=baseline_imports, limits=limits,
        observation_contract=observation_contract, observation_protocol=observation_protocol)


def verify_baseline_import(campaign, *, workspace=None, spec=None, expected_code_sha256=None):
    """Reuse exactly one already graded baseline; never fabricate a new evaluation."""
    import math
    from .research_evaluator import score_predictions
    try:
        spec = spec or campaign.manifest['baseline_import']
        if type(spec.get('version')) is not int or spec['version'] != 1:
            raise ValueError('invalid baseline import version')
        if type(spec.get('source_evaluation')) is not int or spec['source_evaluation'] < 1:
            raise ValueError('invalid baseline evaluation identity')
        required = [Path(spec['source_root']) / 'campaign.json', Path(spec['source_root']) / 'journal.jsonl',
                    Path(spec['source_pins']), Path(spec['report']), Path(spec['measurement'])]
        if any(str(path) not in spec['anchors'] for path in required):
            raise ValueError('missing required baseline anchor')
        for path, sha in spec['anchors'].items():
            if file_sha(plain_path(path)) != sha:
                raise ValueError('baseline import anchor changed')
        root = plain_path(spec['source_root']).resolve()
        manifest = json.loads((root / 'campaign.json').read_text(encoding='utf-8'))
        old = _ArchivedCampaign(root, manifest)
        old.source_file_map = {}
        pins = json.loads(Path(spec['source_pins']).read_text(encoding='utf-8'))
        for index, pin in enumerate(pins):
            name = Path(pin['path']).name
            artifact = Path(spec['source_archive']) / f'{index:02d}-{name}'
            if file_sha(artifact) != pin['sha256'] or artifact.stat().st_size != pin['bytes']:
                raise ValueError('baseline archive changed')
            if name in manifest['implementation'] and pin['sha256'] == manifest['implementation'][name]:
                old.source_file_map[name] = artifact
        old.verify()
        if manifest['campaign_id'] != spec['source_campaign_id']:
            raise ValueError('baseline source identity changed')
        report = json.loads(Path(spec['report']).read_text(encoding='utf-8'))
        measured = json.loads(Path(spec['measurement']).read_text(encoding='utf-8'))
        if measured != report['baseline'] or measured['valid'] is not True:
            raise ValueError('baseline measurement/report disagree')
        p = measured['pins']
        expected = {'task_manifest_sha256': campaign.task.manifest_sha256,
            'code_sha256': expected_code_sha256 or campaign.manifest['baseline_sha256'],
            'features_sha256': file_sha(campaign.task.dev_features), 'image_id': campaign.manifest['image'],
            'split': 'dev', 'verifier_sha256': file_sha(Path(__file__).with_name('research_evaluator.py')),
            'boundary_sha256': {name: file_sha(Path(__file__).with_name(name))
                for name in ('research_task.py', 'rsi_branch.py')}}
        if any(p.get(k) != v for k, v in expected.items()) or measured['feedback_type'] != 'host_rmse':
            raise ValueError('baseline task/code/features/image/verifier binding changed')
        records = [e['payload']['budget_state']['evaluations'] for e in old.events()
            if 'budget_state' in e['payload']]
        record = records[-1][spec['source_evaluation'] - 1]
        if (record['evaluation'] != spec['source_evaluation'] or record['stage'] != 'baseline' or
                record['status'] != 'completed' or record['valid'] is not True or record['pins'] != p):
            raise ValueError('baseline host evaluation is incomplete')
        predictions = plain_path(measured['artifacts']['predictions'])
        if file_sha(predictions) != measured['artifacts']['predictions_sha256']:
            raise ValueError('baseline prediction artifact changed')
        scored = score_predictions(predictions, campaign.task.labels('dev'))
        if any(not math.isclose(measured[k], scored[k], rel_tol=1e-12, abs_tol=1e-12) for k in ('score', 'rmse')):
            raise ValueError('baseline numeric score changed')
        if workspace is not None and (set(inventory(workspace)) != {'train.py'} or
                file_sha(Path(workspace) / 'train.py') != p['code_sha256']):
            raise ValueError('baseline workspace changed')
        return deepcopy(measured)
    except ResearchIntegrityError:
        raise
    except (ValueError, OSError, KeyError, TypeError, IndexError) as exc:
        raise ResearchIntegrityError('invalid pinned baseline import') from exc
