"""Host-recomputed CSV RMSE with fail-closed, pinned Docker execution."""
from __future__ import annotations

import asyncio
import csv
import math
import os
from pathlib import Path
import re
import shutil
import time
from uuid import uuid4

from .rsi_branch import file_sha, inventory, plain_path, write_json
from .rsi_runtime import TrustedEvaluator


class ResearchBlocked(RuntimeError):
    """Required Docker boundary unavailable; never fall back to host execution."""


def resolve_docker(docker_command=None):
    """Resolve a fixed local Docker CLI prefix; WSL is execution, not host fallback."""
    if docker_command is not None:
        value = tuple(docker_command)
        if not value or any(not isinstance(v, str) or not v or any(ord(c) < 32 for c in v) for v in value):
            raise ValueError('invalid Docker command prefix')
        return value
    if shutil.which('docker'):
        return ('docker',)
    if os.name == 'nt' and shutil.which('wsl.exe'):
        return ('wsl.exe', '-d', 'Ubuntu-20.04', '--', 'docker')
    raise ResearchBlocked('Docker executable unavailable')


def score_predictions(path, labels):
    path = plain_path(path)
    if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError('missing or oversized predictions')
    expected = dict(labels)
    if len(expected) != len(labels) or not expected:
        raise ValueError('invalid host labels')
    predictions = {}
    with path.open(encoding='utf-8', newline='') as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ['id', 'prediction']:
            raise ValueError('prediction header must be id,prediction')
        for row in reader:
            identity = row.get('id')
            if None in row or identity not in expected or identity in predictions:
                raise ValueError('prediction ids differ')
            try:
                value = float(row['prediction'])
            except (ValueError, TypeError, OverflowError):
                raise ValueError('prediction must be finite numeric') from None
            if not math.isfinite(value):
                raise ValueError('prediction must be finite numeric')
            predictions[identity] = value
    if set(predictions) != set(expected):
        raise ValueError('prediction ids differ')
    # hypot avoids overflow in squared residuals; reject nonfinite differences.
    residuals = [(predictions[k] - expected[k]) / math.sqrt(len(expected)) for k in expected]
    rmse = math.hypot(*residuals)
    if not math.isfinite(rmse):
        raise ValueError('nonfinite RMSE')
    return {'rmse': rmse, 'score': 1.0 / (1.0 + rmse)}


class DockerRunner:
    def __init__(self, *, image, timeout=60, cpus=1.0, memory_mb=512, pids_limit=64, docker_command=None):
        if not isinstance(image, str) or not re.fullmatch(r'(?:sha256:|[A-Za-z0-9._:/-]+@sha256:)[0-9a-f]{64}', image):
            raise ValueError('image must be an immutable local image ID or repository digest')
        if (type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 3600
                or type(cpus) not in (int, float) or not math.isfinite(cpus) or not 0 < cpus <= 8
                or type(memory_mb) is not int or not 64 <= memory_mb <= 8192
                or type(pids_limit) is not int or not 8 <= pids_limit <= 256):
            raise ValueError('invalid container resource limits')
        self.image, self.timeout = image, timeout
        self.cpus, self.memory_mb, self.pids_limit = cpus, memory_mb, pids_limit
        self.image_id = None
        # Resolve lazily so offline validation/scoring needs no Docker install.
        self.docker_command = tuple(docker_command) if docker_command is not None else None

    def _mount_path(self, path):
        raw = str(plain_path(path).resolve())
        if raw.startswith(('\\\\', '//')) or ',' in raw or any(ord(c) < 32 for c in raw):
            raise ValueError('unsupported container mount path')
        command = resolve_docker(self.docker_command)
        if Path(command[0]).name.lower() in ('wsl', 'wsl.exe'):
            if not re.match(r'^[A-Za-z]:[\\/]', raw):
                raise ValueError('WSL bind mounts require a local Windows drive')
            return '/mnt/' + raw[0].lower() + '/' + raw[3:].replace('\\', '/')
        return raw

    async def _command(self, *args, timeout):
        try:
            process = await asyncio.create_subprocess_exec(*resolve_docker(self.docker_command), *args,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        except OSError:
            raise ResearchBlocked('Docker executable unavailable') from None
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
            return process.returncode, stdout, stderr
        finally:
            if process.returncode is None:
                process.kill()
                await process.communicate()

    async def verify_image(self):
        try:
            rc, stdout, _ = await self._command('image', 'inspect', '--format', '{{.Id}}', self.image, timeout=10)
        except asyncio.TimeoutError:
            raise ResearchBlocked('Docker daemon/image inspection timed out') from None
        image_id = stdout.decode('utf-8', errors='replace').strip()
        if rc or not re.fullmatch(r'sha256:[0-9a-f]{64}', image_id):
            raise ResearchBlocked('pinned Docker image unavailable locally')
        if self.image.startswith('sha256:') and image_id != self.image:
            raise ResearchBlocked('Docker image identity differs')
        if self.image_id is not None and image_id != self.image_id:
            raise ResearchBlocked('Docker image identity changed')
        self.image_id = image_id
        return image_id

    async def _remove_owned(self, name, cid, cidfile):
        if cid is None and cidfile.is_file():
            candidate = cidfile.read_text(encoding='utf-8').strip()
            if re.fullmatch(r'[0-9a-f]{64}', candidate):
                cid = candidate
        if cid is None:
            # A cancelled create may have reached the daemon before its CLI wrote
            # cidfile. Inspect the exact ownership label before deleting by ID.
            rc, stdout, _ = await self._command('inspect', '--format',
                '{{.Id}} {{index .Config.Labels "my-agent-harness.owner"}}', name, timeout=10)
            parts = stdout.decode('utf-8', errors='replace').strip().split()
            if rc or len(parts) != 2 or not re.fullmatch(r'[0-9a-f]{64}', parts[0]) or parts[1] != name:
                raise ResearchBlocked('cannot confirm owned container cleanup')
            cid = parts[0]
        rc, _, _ = await self._command('rm', '-f', cid, timeout=15)
        if rc:
            raise ResearchBlocked('Docker container cleanup failed')
        # Successful daemon rm confirms removal, including stopping a running CID.

    async def run(self, code, train, features, output, budget):
        image_id = await self.verify_image()
        name = 'harness-research-' + uuid4().hex
        cid, cidfile = None, output.parent / 'container.cid'
        for path in (code, train, features, output):
            # --mount uses comma separators and cannot represent these names safely.
            if ',' in str(path) or any(ord(c) < 32 for c in str(path)) or str(path).startswith(('\\\\', '//')):
                raise ValueError('unsupported container mount path')
            plain_path(path)
        if os.name == 'posix':
            output.chmod(0o777)
        predictions = output / 'predictions.csv'
        predictions.touch(exist_ok=False)
        predictions.chmod(0o666)
        mounts = []
        for source, target in ((code, '/app/train.py'), (train, '/data/train.csv'),
                               (features, '/data/predict_features.csv')):
            mounts += ['--mount', f'type=bind,source={self._mount_path(source)},target={target},readonly']
        # Only one bounded file is writable; a directory bind would let generated
        # code fill the host disk with undeclared files before post-validation.
        mounts += ['--mount', f'type=bind,source={self._mount_path(predictions)},target=/output/predictions.csv']
        created = False
        try:
            # Set before create: cancellation may leave the daemon-side create completed.
            created = True
            rc, stdout, _ = await self._command('create', '--name', name, '--cidfile', self._mount_path(cidfile),
                '--label', f'my-agent-harness.owner={name}', '--pull', 'never', '--network', 'none',
                '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--cpus', str(self.cpus), '--memory', f'{self.memory_mb}m', '--memory-swap', f'{self.memory_mb}m',
                '--pids-limit', str(self.pids_limit), '--user', '65534:65534', '--log-driver', 'none',
                '--ulimit', 'fsize=16777216:16777216', '--ulimit', 'nofile=64:64',
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m', '--workdir', '/app',
                *mounts, '--entrypoint', 'python', image_id, '-I', '-B', '/app/train.py',
                timeout=min(20, budget.remaining()))
            if rc:
                raise ResearchBlocked('Docker sandbox creation failed')
            cid = stdout.decode('utf-8', errors='replace').strip()
            if not re.fullmatch(r'[0-9a-f]{64}', cid):
                cid = None
                raise ResearchBlocked('Docker returned invalid container identity')
            rc, _, _ = await self._command('start', name, timeout=min(20, budget.remaining()))
            if rc:
                raise ResearchBlocked('Docker sandbox start failed')
            rc, stdout, _ = await self._command('wait', name, timeout=min(self.timeout, budget.remaining()))
            if rc:
                raise ResearchBlocked('Docker wait failed')
            try:
                exit_code = int(stdout.strip())
            except ValueError:
                raise ResearchBlocked('Docker returned invalid exit status') from None
            return {'image_id': image_id, 'exit_code': exit_code}
        finally:
            if created:
                # Independent of exhausted parent time; remove the owned container,
                # not merely its client process. Cleanup failure invalidates the run.
                cleanup = asyncio.create_task(self._remove_owned(name, cid, cidfile))
                try:
                    try:
                        await asyncio.shield(cleanup)
                    except (asyncio.TimeoutError, OSError):
                        raise ResearchBlocked('Docker container cleanup failed') from None
                except asyncio.CancelledError:
                    try:
                        await cleanup
                    except (asyncio.TimeoutError, OSError):
                        raise ResearchBlocked('Docker container cleanup failed') from None
                    raise


class NumericPredictionEvaluator(TrustedEvaluator):
    feedback_type = 'host_rmse'

    def __init__(self, task, *, image, output_dir, split='dev', timeout=60, cpus=1.0, memory_mb=512, pids_limit=64, docker_command=None):
        if split != 'dev':
            raise ValueError('online evaluator accepts development split only')
        self.task, self.output_dir = task, plain_path(output_dir).resolve()
        self.runner = DockerRunner(image=image, timeout=timeout, cpus=cpus, memory_mb=memory_mb, pids_limit=pids_limit, docker_command=docker_command)
        self._verifier_sha = file_sha(__file__)
        self._boundary_pins = {name: file_sha(Path(__file__).with_name(name))
                               for name in ('research_task.py', 'rsi_branch.py')}
        self._task_sha = task.manifest_sha256
        self._policy = (image, timeout, cpus, memory_mb, pids_limit)
        self._docker_command = tuple(docker_command) if docker_command is not None else None
        self._final_used = False
        self.verify()

    def verify(self):
        self.task.verify()
        if self.task.manifest_sha256 != self._task_sha or file_sha(__file__) != self._verifier_sha:
            raise ValueError('research verifier or task identity changed')
        if any(file_sha(Path(__file__).with_name(name)) != sha for name, sha in self._boundary_pins.items()):
            raise ValueError('research task adapter or execution boundary changed')
        if isinstance(self.runner, DockerRunner) and (self.runner.image, self.runner.timeout, self.runner.cpus,
                self.runner.memory_mb, self.runner.pids_limit) != self._policy:
            raise ValueError('container policy changed')
        if isinstance(self.runner, DockerRunner) and self.runner.docker_command != self._docker_command:
            raise ValueError('container command changed')

    async def evaluate(self, workspace, budget, llm):
        return await self._evaluate(workspace, budget, 'dev')

    async def evaluate_final(self, workspace, budget):
        """Host-only one-shot score. Caller must never feed this into search."""
        if self._final_used:
            raise ValueError('final evaluation already attempted')
        self._final_used = True
        return await self._evaluate(workspace, budget, 'final')

    async def _evaluate(self, workspace, budget, split):
        self.verify()
        workspace = plain_path(workspace).resolve()
        if set(inventory(workspace)) != {'train.py'}:
            raise ValueError('candidate workspace must contain only train.py')
        record = budget.begin_evaluation()
        started = time.monotonic()
        directory = self.output_dir / uuid4().hex
        directory.mkdir(parents=True)
        code = directory / 'train.py'
        code.write_bytes((workspace / 'train.py').read_bytes())
        output = directory / 'output'
        output.mkdir()
        features = self.task.dev_features if split == 'dev' else self.task.final_features
        pins = {'task_manifest_sha256': self._task_sha, 'verifier_sha256': self._verifier_sha,
                'boundary_sha256': dict(self._boundary_pins),
                'code_sha256': file_sha(code), 'features_sha256': file_sha(features), 'split': split}
        result = {'valid': False, 'score': None, 'rmse': None, 'feedback_type': self.feedback_type,
                  'pins': pins, 'artifacts': {}, 'feedback': 'candidate not scorable'}
        try:
            try:
                execution = await self.runner.run(code, self.task.train_csv, features, output, budget)
            except asyncio.TimeoutError:
                result['feedback'] = 'candidate execution timed out'
            else:
                pins['image_id'] = execution['image_id']
                if execution['exit_code'] != 0:
                    result['feedback'] = 'candidate execution failed'
                else:
                    try:
                        if set(inventory(output)) != {'predictions.csv'}:
                            raise ValueError('output must contain only predictions.csv')
                        predictions = plain_path(output / 'predictions.csv')
                        measured = score_predictions(predictions, self.task.labels(split))
                        result.update(valid=True, **measured,
                                      feedback=f"Host {split} RMSE: {measured['rmse']:.12g}; utility: {measured['score']:.12g}")
                        result['artifacts'] = {'predictions': str(predictions), 'predictions_sha256': file_sha(predictions)}
                    except (ValueError, OSError, csv.Error):
                        result['feedback'] = 'invalid predictions: require exact ids, finite numbers and declared CSV only'
            self.verify()
            write_json(directory / 'evaluation.json', result)
            record.update(status='completed', valid=result['valid'], pins=pins)
            return result
        except BaseException as exc:
            record.update(status='invalid', error_type=type(exc).__name__)
            raise
        finally:
            record['wall_seconds'] = time.monotonic() - started
