"""Pinned HumanEval calibration tasks, with host-only evaluation material.

This is protocol separation, not an OS sandbox. HumanEval is public and may
already occur in model training data; these tasks measure pipeline operation.
No provider requests are made here. Any failed task blocks the whole batch.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import doctest
import gzip
import hashlib
import inspect
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import urllib.request
import uuid


COMMIT = "b9453e8f1865df0a0057a1383767caa400acfbcf"
DATA_BLOB = "998d25196e17af24daf9b6cb3a975fe752528e46"
DATA_SHA256 = "b796127e635a67f93fb35c04f4cb03cf06f38c8072ee7cee8833d7bee06979ef"
LICENSE_SHA256 = "bcba3de214851cce46ed5af42d6698044616eeace887c3231bc7a20474ab639e"
SPLITS = {"train": (7, 11, 27), "val": (32, 45), "test": (1, 6, 9, 20, 33, 40)}
FROZEN_IDS = tuple(f"HumanEval/{n}" for ids in SPLITS.values() for n in ids)
SCHEDULING_SEEDS = (23, 47)


class TaskPreparationError(ValueError):
    """A frozen task is invalid, changed, or failed local preflight."""


# No pickle/eval/object hooks: only bounded primitive/list/tuple values cross
# the candidate boundary. The same codec is independently loaded in each process.
CODEC = '''import json, math
MAX_RPC_BYTES = 1000000
def pack(value, depth=0):
    if depth > 32:
        raise ValueError('ProtocolDepth')
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if type(value) in (list, tuple) and len(value) <= 10000:
        return {'type': 'tuple' if type(value) is tuple else 'list', 'items': [pack(x, depth + 1) for x in value]}
    raise ValueError('UnsupportedValue')
def unpack(value, depth=0):
    if depth > 32:
        raise ValueError('ProtocolDepth')
    if value is None or type(value) in (bool, int, str):
        return value
    if type(value) is float and math.isfinite(value):
        return value
    if (type(value) is dict and set(value) == {'type', 'items'} and value['type'] in ('tuple', 'list')
            and type(value['items']) is list and len(value['items']) <= 10000):
        items = [unpack(x, depth + 1) for x in value['items']]
        return tuple(items) if value['type'] == 'tuple' else items
    raise ValueError('UnsupportedValue')
def wire(value):
    text = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(',', ':')) + '\\n'
    if len(text.encode('utf-8')) > MAX_RPC_BYTES:
        raise ValueError('ProtocolSize')
    return text
'''


WORKER = CODEC + '''import argparse, os, pathlib, sys
p = argparse.ArgumentParser()
p.add_argument('--entry-point', required=True)
a = p.parse_args()
# Save protocol output before candidate code runs. Candidate prints are discarded
# by the host, and cannot become test feedback or corrupt ordinary RPC traffic.
channel = os.fdopen(os.dup(sys.stdout.fileno()), 'w', encoding='utf-8', buffering=1)
sys.stdout = sys.stderr
def send(value):
    channel.write(wire(value))
    channel.flush()
candidate_namespace = {'__name__': '__candidate__'}
try:
    exec(compile(pathlib.Path('solution.py').read_text(encoding='utf-8'), 'solution.py', 'exec'), candidate_namespace)
    candidate = candidate_namespace[a.entry_point]
    if not callable(candidate):
        raise TypeError('NotCallable')
except BaseException:
    send({'ready': False, 'error_type': 'CandidateLoadError'})
    sys.exit(1)
send({'ready': True})
for line in sys.stdin:
    try:
        if len(line.encode('utf-8')) > MAX_RPC_BYTES:
            raise ValueError('ProtocolSize')
        request = json.loads(line)
        if type(request) is not dict or set(request) != {'id', 'args', 'kwargs'} or type(request['id']) is not int:
            raise ValueError('ProtocolShape')
        args = unpack(request['args'])
        kwargs = request['kwargs']
        if type(args) is not tuple or type(kwargs) is not dict or any(type(k) is not str for k in kwargs):
            raise ValueError('ProtocolArguments')
        result = candidate(*args, **{k: unpack(v) for k, v in kwargs.items()})
        send({'id': request['id'], 'ok': True, 'value': pack(result)})
    except BaseException:
        send({'id': request.get('id') if type(locals().get('request')) is dict else None,
              'ok': False, 'error_type': 'CandidateCallError'})
'''


# The immutable host executes only official prompt/check code; branch solution.py
# is executed exclusively by a separate persistent worker with no task payload.
EVALUATOR = CODEC + '''import argparse, ast, doctest, pathlib, subprocess, sys, os, random
p = argparse.ArgumentParser()
p.add_argument('--task', required=True)
p.add_argument('--stage', choices=['visible', 'final'], required=True)
a = p.parse_args()
t = json.loads(pathlib.Path(a.task).read_text(encoding='utf-8'))
expected_fields = {'task_id', 'entry_point', 'prompt'} | ({'test'} if a.stage == 'final' else set())
if set(t) != expected_fields:
    print(json.dumps({'passed': False, 'error_type': 'PayloadSchemaError'}))
    sys.exit(1)
class CandidateWorker:
    def __init__(self):
        self.job = _WindowsJob() if os.name == 'nt' else None
        self.process = None
        self.counter = 0
        try:
            env = {k: v for k, v in os.environ.items() if not any(word in k.upper()
                   for word in ('KEY', 'TOKEN', 'SECRET', 'PASSWORD', 'CREDENTIAL', 'AUTH'))}
            self.process = subprocess.Popen([sys.executable, '-I', str(pathlib.Path(__file__).with_name('candidate_worker.py')),
                '--entry-point', t['entry_point']], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding='utf-8', env=env)
            if self.job:
                self.job.assign(self.process)
            if self.read() != {'ready': True}:
                raise ValueError('CandidateLoadError')
        except BaseException:
            self.close()
            raise
    def read(self):
        line = self.process.stdout.readline(MAX_RPC_BYTES + 1)
        if not line or not line.endswith('\\n') or len(line.encode('utf-8')) > MAX_RPC_BYTES:
            raise ValueError('ProtocolReadError')
        return json.loads(line)
    def __call__(self, *args, **kwargs):
        self.counter += 1
        self.process.stdin.write(wire({'id': self.counter, 'args': pack(args), 'kwargs': {k: pack(v) for k, v in kwargs.items()}}))
        self.process.stdin.flush()
        response = self.read()
        if (type(response) is not dict or set(response) != {'id', 'ok', 'value'} or response['id'] != self.counter
                or response['ok'] is not True):
            raise ValueError('CandidateCallError')
        return unpack(response['value'])
    def close(self):
        if self.job:
            self.job.close()
            self.job = None
        if self.process is not None:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait()
            for stream in (self.process.stdin, self.process.stdout):
                if stream:
                    try:
                        stream.close()
                    except OSError:
                        pass
worker = None
ok = False
error_type = None
try:
    worker = CandidateWorker()
    ns = {'__name__': '__official_public_prompt__'}
    exec(compile(t['prompt'], '<official-public-prompt>', 'exec'), ns)
    ns[t['entry_point']] = worker
    if a.stage == 'visible':
        fn = next(n for n in ast.parse(t['prompt']).body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == t['entry_point'])
        dt = doctest.DocTestParser().get_doctest(ast.get_docstring(fn), ns, t['task_id'], '<public-prompt>', 0)
        runner = doctest.DocTestRunner()
        result = runner.run(dt, out=lambda text: None)
        ok = result.attempted > 0 and result.failed == 0
    else:
        exec(compile(t['test'], '<host-final-tests>', 'exec'), ns)
        random.seed(0)
        ns['check'](ns[t['entry_point']])
        ok = True
except BaseException:
    error_type = 'EvaluationError'
    ok = False
finally:
    if worker is not None:
        worker.close()
print(json.dumps({'passed': bool(ok), 'error_type': error_type}))
sys.exit(0 if ok else 1)
'''


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _json(path: Path, value) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _check(path: Path, expected: str) -> None:
    if not path.is_file() or _sha(path) != expected:
        raise TaskPreparationError(f"hash mismatch: {path}")


class _WindowsJob:
    """Own evaluation descendants until every process is terminated."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        class Basic(ctypes.Structure):
            _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
                        ("flags", wintypes.DWORD), ("min_ws", ctypes.c_size_t),
                        ("max_ws", ctypes.c_size_t), ("active", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
                        ("scheduling", wintypes.DWORD)]
        class IO(ctypes.Structure):
            _fields_ = [(name, ctypes.c_uint64) for name in
                        ("reads", "writes", "other", "read_bytes", "write_bytes", "other_bytes")]
        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        limits = Extended()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.handle or not self.kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            if self.handle:
                self.kernel.CloseHandle(self.handle)
            raise TaskPreparationError("cannot create owned evaluation Job Object")

    def assign(self, process):
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            process.kill()
            process.wait()
            raise TaskPreparationError("cannot assign evaluation process to Job Object")

    def close(self):
        self.kernel.TerminateJobObject(self.handle, 1)
        self.kernel.CloseHandle(self.handle)


# Share our already-tested owned Job implementation with the standalone checker.
# No project import or source lookup happens in an evaluation process.
EVALUATOR = "import os\nTaskPreparationError = RuntimeError\n" + inspect.getsource(_WindowsJob) + "\n" + EVALUATOR


def _evaluate(argv: tuple[str, ...], cwd: Path, timeout: float = 10) -> int:
    env = {k: v for k, v in os.environ.items()
           if not any(word in k.upper() for word in ("KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "AUTH"))}
    job = _WindowsJob() if os.name == "nt" else None
    process = None
    try:
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=os.name != "nt")
        if job:
            job.assign(process)
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise TaskPreparationError("evaluation timeout") from exc
    finally:
        # Always clean descendants, even if the direct child has already exited.
        if job:
            job.close()
        elif process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        if process is not None:
            process.wait()
    return int(process.returncode == 0)


@dataclass(frozen=True)
class PreparedTask:
    task_id: str
    split: str
    entry_point: str
    source_dir: Path
    goal: str
    visible_argv: tuple[str, ...]
    final_argv: tuple[str, ...]
    source_sha: str
    host_evaluator_sha: str
    preflight_proof: dict
    _manifest: Path = field(repr=False)
    _manifest_sha: str = field(repr=False)
    _evaluator: Path = field(repr=False)
    _host_task: Path = field(repr=False)
    _host_task_sha: str = field(repr=False)
    _final_task: Path = field(repr=False)
    _final_task_sha: str = field(repr=False)
    _worker: Path = field(repr=False)
    _worker_sha: str = field(repr=False)

    def verify(self) -> None:
        _check(self._manifest, self._manifest_sha)
        _check(self.source_dir / "solution.py", self.source_sha)
        if sorted(p.name for p in self.source_dir.iterdir()) != ["solution.py"]:
            raise TaskPreparationError("unexpected source files")
        _check(self._evaluator, self.host_evaluator_sha)
        _check(self._host_task, self._host_task_sha)
        _check(self._final_task, self._final_task_sha)
        if self._worker.resolve() != self._evaluator.with_name("candidate_worker.py").resolve():
            raise TaskPreparationError("worker path differs from checker dependency")
        _check(self._worker, self._worker_sha)
        _check(self._worker, hashlib.sha256(WORKER.encode("utf-8")).hexdigest())
        _check(self._evaluator, hashlib.sha256(EVALUATOR.encode("utf-8")).hexdigest())
        private_task = json.loads(self._host_task.read_text(encoding="utf-8"))
        final_task = json.loads(self._final_task.read_text(encoding="utf-8"))
        if set(private_task) != {"task_id", "entry_point", "prompt"} or set(final_task) != {*private_task, "test"}:
            raise TaskPreparationError("visible/final payload schema drift")
        if any(final_task[key] != value for key, value in private_task.items()):
            raise TaskPreparationError("visible/final public identity differs")
        if self._host_task == self._final_task:
            raise TaskPreparationError("visible/final payloads must be separate files")
        if self.source_sha != hashlib.sha256(private_task["prompt"].encode("utf-8")).hexdigest():
            raise TaskPreparationError("source prompt hash mismatch")
        expected_split = next(k for k, ids in SPLITS.items() if int(self.task_id.split("/")[1]) in ids)
        if self.split != expected_split or private_task["task_id"] != self.task_id or private_task["entry_point"] != self.entry_point:
            raise TaskPreparationError("task identity drift")
        if self.goal != "Implement solution.py for the following public task:\n\n" + private_task["prompt"]:
            raise TaskPreparationError("goal drift")
        expected_argv = (self.visible_argv[0], "-I", str(self._evaluator), "--task", str(self._host_task), "--stage", "visible")
        expected_final = (*expected_argv[:4], str(self._final_task), "--stage", "final")
        if self.visible_argv != expected_argv or self.final_argv != expected_final:
            raise TaskPreparationError("evaluator argv drift")
        if self.preflight_proof.get("visible_cases") != _visible_count(private_task) or self.preflight_proof["visible_cases"] < 1:
            raise TaskPreparationError("visible case drift")
        if any(self.preflight_proof.get(f"{stage}_{kind}") != expected for stage in ("visible", "final")
               for kind, expected in (("empty", 0), ("gold", 1))):
            raise TaskPreparationError("preflight proof invalid")
        manifest = json.loads(self._manifest.read_text(encoding="utf-8"))
        for path, digest in manifest.get("private_source_hashes", {}).items():
            _check(Path(path), digest)


def _visible_count(row: dict) -> int:
    try:
        fn = next(n for n in ast.parse(row["prompt"]).body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == row["entry_point"])
        return len(doctest.DocTestParser().get_examples(ast.get_docstring(fn) or ""))
    except (StopIteration, SyntaxError, ValueError) as exc:
        raise TaskPreparationError(f"invalid prompt: {row['task_id']}") from exc


def _prepare_rows(output, source_cache, python_executable, rows, provenance) -> Path:
    output, source_cache = Path(output).resolve(), Path(source_cache).resolve()
    if output.exists():
        raise FileExistsError(output)
    if source_cache == output or source_cache.is_relative_to(output):
        raise TaskPreparationError("private cache must be outside task output")
    selected = [row for row in rows if row["task_id"] in FROZEN_IDS]
    if len(selected) != len(FROZEN_IDS) or {r["task_id"] for r in selected} != set(FROZEN_IDS):
        raise TaskPreparationError("frozen IDs missing or duplicated")
    if len({hashlib.sha256(r["prompt"].encode()).hexdigest() for r in selected}) != len(selected):
        raise TaskPreparationError("duplicate task content")
    counts = {r["task_id"]: _visible_count(r) for r in selected}
    absent = [task_id for task_id, n in counts.items() if n == 0]
    if absent:
        raise TaskPreparationError("no visible examples: " + ", ".join(absent))
    source_cache.mkdir(parents=True, exist_ok=True)
    private = source_cache / ("prepared-" + uuid.uuid4().hex)
    private.mkdir()
    evaluator = private / "evaluator.py"
    evaluator.write_bytes(EVALUATOR.encode("utf-8"))
    worker = private / "candidate_worker.py"
    worker.write_bytes(WORKER.encode("utf-8"))
    entries = []
    # Build in a temporary sibling; publish only after every preflight passes.
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="humaneval-preflight-", dir=output.parent) as temp:
        staging = Path(temp)
        for task_id in FROZEN_IDS:
            row = next(r for r in selected if r["task_id"] == task_id)
            name = task_id.replace("/", "-")
            host_task = private / f"{name}-visible.json"
            final_task = private / f"{name}-final.json"
            _json(host_task, {k: row[k] for k in ("task_id", "prompt", "entry_point")})
            _json(final_task, {k: row[k] for k in ("task_id", "prompt", "test", "entry_point")})
            source = staging / name
            source.mkdir()
            solution = source / "solution.py"
            solution.write_bytes(row["prompt"].encode("utf-8"))
            visible = (str(python_executable), "-I", str(evaluator), "--task", str(host_task), "--stage", "visible")
            final = (*visible[:4], str(final_task), "--stage", "final")
            proof = {"visible_cases": counts[task_id]}
            for kind, code in (("empty", row["prompt"]), ("gold", row["prompt"] + row["canonical_solution"])):
                solution.write_bytes(code.encode("utf-8"))
                proof[f"visible_{kind}"] = _evaluate(visible, source)
                proof[f"final_{kind}"] = _evaluate(final, source)
            if any(proof[f"{stage}_{kind}"] != expected for stage in ("visible", "final")
                   for kind, expected in (("empty", 0), ("gold", 1))):
                raise TaskPreparationError(f"empty/gold preflight failed: {task_id}: {proof}")
            solution.write_bytes(row["prompt"].encode("utf-8"))
            split = next(k for k, ids in SPLITS.items() if int(task_id.split("/")[1]) in ids)
            entries.append(dict(task_id=task_id, split=split, entry_point=row["entry_point"],
                source_dir=str(output / name), goal="Implement solution.py for the following public task:\n\n" + row["prompt"],
                visible_argv=visible, final_argv=final, source_sha=_sha(solution),
                host_evaluator_sha=_sha(evaluator), host_task=str(host_task), host_task_sha=_sha(host_task),
                final_task=str(final_task), final_task_sha=_sha(final_task),
                host_worker=str(worker), host_worker_sha=_sha(worker),
                preflight_proof=proof))
        manifest = dict(schema_version=2, preparation_protocol="host_checker_candidate_worker_typed_json_rpc_v3", host_checker_random_seed=0,
            source=provenance, scheduling_seeds=SCHEDULING_SEEDS,
            limitations=["public short-code pipeline calibration", "protocol separation only; no OS sandbox", "no leaderboard or long-horizon generalization claim"],
            private_source_hashes=provenance.get("private_source_hashes", {}), tasks=entries)
        _json(staging / "manifest.json", manifest)
        (staging / "manifest.sha256").write_text(_sha(staging / "manifest.json"), encoding="ascii")
        # mkdir is exclusive: a concurrent preparation cannot overwrite output.
        output.mkdir()
        for path in staging.iterdir():
            path.rename(output / path.name)
    return output / "manifest.json"


def prepare_humaneval(output, source_cache, python_executable) -> Path:
    """Download/verify pinned public source privately and preflight frozen IDs."""
    cache = Path(source_cache).resolve()
    if Path(output).exists():
        raise FileExistsError(output)
    cache.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for remote, filename, digest in (("data/HumanEval.jsonl.gz", "HumanEval.jsonl.gz", DATA_SHA256),
                                      ("LICENSE", "LICENSE", LICENSE_SHA256)):
        path = cache / filename
        if not path.exists():
            request = urllib.request.urlopen(f"https://raw.githubusercontent.com/openai/human-eval/{COMMIT}/{remote}", timeout=30)
            with request:
                content = request.read()
            if hashlib.sha256(content).hexdigest() != digest:
                raise TaskPreparationError("download hash mismatch")
            with path.open("xb") as handle:
                handle.write(content)
        _check(path, digest)
        hashes[str(path)] = digest
    data = (cache / "HumanEval.jsonl.gz").read_bytes()
    if hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest() != DATA_BLOB:
        raise TaskPreparationError("Git blob mismatch")
    rows = [json.loads(line) for line in gzip.decompress(data).decode("utf-8").splitlines()]
    return _prepare_rows(output, cache, python_executable, rows,
        dict(repository="https://github.com/openai/human-eval", commit=COMMIT, git_blob=DATA_BLOB,
             data_sha256=DATA_SHA256, license="MIT", license_sha256=LICENSE_SHA256,
             private_source_hashes=hashes))


def load_tasks(manifest_path) -> list[PreparedTask]:
    manifest_path = Path(manifest_path).resolve()
    digest = manifest_path.with_name("manifest.sha256").read_text(encoding="ascii").strip()
    _check(manifest_path, digest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != 2 or manifest.get("preparation_protocol") != "host_checker_candidate_worker_typed_json_rpc_v3"
            or manifest.get("host_checker_random_seed") != 0):
        raise TaskPreparationError("obsolete or unknown evaluator protocol; regenerate before payment")
    rows = manifest["tasks"]
    if tuple(r["task_id"] for r in rows) != FROZEN_IDS:
        raise TaskPreparationError("frozen task IDs changed")
    result = []
    for row in rows:
        args = {k: row[k] for k in ("task_id", "split", "entry_point", "goal", "source_sha", "host_evaluator_sha", "preflight_proof")}
        args.update(source_dir=Path(row["source_dir"]), visible_argv=tuple(row["visible_argv"]), final_argv=tuple(row["final_argv"]),
            _manifest=manifest_path, _manifest_sha=digest, _evaluator=Path(row["visible_argv"][2]),
            _host_task=Path(row["host_task"]), _host_task_sha=row["host_task_sha"],
            _final_task=Path(row["final_task"]), _final_task_sha=row["final_task_sha"],
            _worker=Path(row["host_worker"]), _worker_sha=row["host_worker_sha"])
        task = PreparedTask(**args)
        task.verify()
        result.append(task)
    return result
