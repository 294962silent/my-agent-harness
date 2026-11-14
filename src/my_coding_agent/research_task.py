"""Fixed CSV regression task. Labels are host-only, never branch source files."""
from __future__ import annotations

import csv
import json
import math
import random

from .rsi_branch import file_sha, plain_path, write_json

BASELINE = '''import csv, statistics
with open('/data/train.csv', newline='') as f:
    rows = list(csv.DictReader(f))
# The target is always the final training column.
target = list(rows[0])[-1]
mean = statistics.mean(float(row[target]) for row in rows)
with open('/data/predict_features.csv', newline='') as f:
    features = list(csv.DictReader(f))
with open('/output/predictions.csv', 'w', newline='') as f:
    writer = csv.writer(f)
    writer.writerow(['id', 'prediction'])
    writer.writerows((row['id'], mean) for row in features)
'''


def _csv(path, fields, rows):
    with path.open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


class ResearchTask:
    def __init__(self, root, manifest):
        self.root = plain_path(root).resolve()
        self.manifest = manifest
        self.manifest_path = self.root / 'manifest.json'
        self.manifest_sha256 = file_sha(self.manifest_path)
        self.source_dir = self.root / 'source'
        self.train_csv = self.root / 'data/train.csv'
        self.dev_features = self.root / 'data/dev_features.csv'
        self.final_features = self.root / 'data/final_features.csv'

    @classmethod
    def prepare(cls, csv_path, root, *, target, id_column=None, seed=23,
                dev_fraction=.2, final_fraction=.2):
        """Create a new immutable split; input CSV is never copied into source."""
        root = plain_path(root).resolve()
        if root.exists():
            raise ValueError('task root must be new')
        if type(seed) is not int or not 0 < dev_fraction < 1 or not 0 < final_fraction < 1 or dev_fraction + final_fraction >= 1:
            raise ValueError('invalid split parameters')
        with plain_path(csv_path).open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            fields = reader.fieldnames or []
            if len(set(fields)) != len(fields) or target not in fields or (id_column is not None and id_column not in fields):
                raise ValueError('invalid target/id/schema')
            features = [v for v in fields if v not in (target, id_column)]
            if not features or 'id' in features or target == 'id' or target == id_column:
                raise ValueError('reserved id column or empty feature schema')
            rows = []
            ids = set()
            for index, row in enumerate(reader):
                identity = row[id_column] if id_column else str(index)
                if not identity or identity in ids or None in row or any(v is None for v in row.values()):
                    raise ValueError('missing values or duplicate ids')
                try:
                    finite = math.isfinite(float(row[target]))
                except (ValueError, TypeError, OverflowError):
                    finite = False
                if not finite:
                    raise ValueError('target must be finite numeric')
                ids.add(identity)
                rows.append({'id': identity, **{k: row[k] for k in features}, target: row[target]})
        ndev, nfinal = int(len(rows)*dev_fraction), int(len(rows)*final_fraction)
        if min(ndev, nfinal, len(rows)-ndev-nfinal) < 1:
            raise ValueError('each split needs at least one row')
        random.Random(seed).shuffle(rows)
        parts = {'dev': rows[:ndev], 'final': rows[ndev:ndev+nfinal], 'train': rows[ndev+nfinal:]}
        for directory in ('source', 'data', 'private'):
            (root / directory).mkdir(parents=True)
        (root / 'source/train.py').write_text(BASELINE, encoding='utf-8', newline='')
        _csv(root / 'data/train.csv', ['id', *features, target], parts['train'])
        for split in ('dev', 'final'):
            _csv(root / f'data/{split}_features.csv', ['id', *features],
                 [{k: row[k] for k in ['id', *features]} for row in parts[split]])
            _csv(root / f'private/{split}_labels.csv', ['id', 'target'],
                 [{'id': row['id'], 'target': row[target]} for row in parts[split]])
        files = {str(p.relative_to(root).as_posix()): file_sha(p)
                 for folder in ('data', 'private') for p in (root / folder).iterdir()}
        manifest = {'version': 1, 'target': target, 'features': features, 'seed': seed,
                    'counts': {k: len(v) for k, v in parts.items()}, 'files': files,
                    'input_sha256': file_sha(csv_path), 'metric': 'rmse', 'utility': '1/(1+rmse)',
                    'baseline_sha256': file_sha(root / 'source/train.py')}
        write_json(root / 'manifest.json', manifest)
        return cls.load(root)

    @classmethod
    def load(cls, root):
        root = plain_path(root).resolve()
        value = cls(root, json.loads((root / 'manifest.json').read_text(encoding='utf-8')))
        value.verify()
        return value

    def verify(self):
        if file_sha(self.manifest_path) != self.manifest_sha256:
            raise ValueError('task manifest changed')
        if json.loads(self.manifest_path.read_text(encoding='utf-8')) != self.manifest:
            raise ValueError('in-memory task manifest changed')
        required = {'data/train.csv', 'data/dev_features.csv', 'data/final_features.csv',
                    'private/dev_labels.csv', 'private/final_labels.csv'}
        if self.manifest.get('version') != 1 or set(self.manifest.get('files', {})) != required:
            raise ValueError('invalid task manifest')
        for relative, expected in self.manifest['files'].items():
            path = plain_path(self.root / relative)
            if not path.is_file() or file_sha(path) != expected:
                raise ValueError('task data changed')

    def labels(self, split):
        if split not in ('dev', 'final'):
            raise ValueError('invalid label split')
        self.verify()
        with (self.root / f'private/{split}_labels.csv').open(encoding='utf-8', newline='') as stream:
            return [(row['id'], float(row['target'])) for row in csv.DictReader(stream)]

    def goal(self):
        return (f"Improve train.py for CSV regression, minimizing development RMSE. "
                f"Feature columns: {self.manifest['features']!r}; target: {self.manifest['target']!r}. "
                "Use Python standard library only. Execution runs in a CPU-only container, without network. "
                "Read /data/train.csv (id, features, target) and /data/predict_features.csv (id, features). "
                "Write /output/predictions.csv with exactly header id,prediction, one finite numeric prediction "
                "per input id. These data paths only exist during host evaluation. Edit only train.py; "
                "host independently computes RMSE. Final labels and scores are unavailable during search.")
