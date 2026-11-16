"""Frozen SWE task catalogs approved by the host, never supplied by the solver.

Hashes detect changes to approved files; this is not a signature or proof against
a malicious host. Catalog creation must verify the official source snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re

OFFICIAL_REPOSITORY = "https://github.com/SWE-bench/swe-bench-tasks"


def _hex(value, length):
    return isinstance(value, str) and re.fullmatch(rf"[0-9a-f]{{{length}}}", value) is not None


def _fields(value, expected, label):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(f"invalid {label} fields")


@dataclass(frozen=True)
class TaskSpec:
    instance_id: str
    repo: str
    base_commit: str
    image: str
    image_id: str
    metadata_sha256: str | None = None
    source_repository: str = OFFICIAL_REPOSITORY
    source_revision: str | None = None

    def __post_init__(self):
        if not isinstance(self.repo, str) or not re.fullmatch(r"[A-Za-z0-9_-]+/[A-Za-z0-9_.-]+", self.repo):
            raise ValueError("invalid catalog repository")
        if self.repo.split("/")[1] in (".", ".."):
            raise ValueError("invalid catalog repository")
        prefix = self.repo.replace("/", "__") + "-"
        if (
            not isinstance(self.instance_id, str)
            or not self.instance_id.startswith(prefix)
            or not re.fullmatch(r"[0-9]+", self.instance_id[len(prefix) :])
        ):
            raise ValueError("catalog instance does not match repository")
        expected_image = "swebench/sweb.eval.x86_64." + self.instance_id.replace("__", "_1776_").lower() + ":latest"
        if self.image != expected_image or not _hex(self.base_commit, 40):
            raise ValueError("invalid official catalog image or base commit")
        if (
            not isinstance(self.image_id, str)
            or not self.image_id.startswith("sha256:")
            or not _hex(self.image_id[7:], 64)
        ):
            raise ValueError("invalid catalog image config digest")
        if self.source_repository != OFFICIAL_REPOSITORY:
            raise ValueError("catalog source must be the official task repository")
        if self.source_revision is not None and not _hex(self.source_revision, 40):
            raise ValueError("catalog source revision must be a commit")
        if self.metadata_sha256 is not None and not _hex(self.metadata_sha256, 64):
            raise ValueError("invalid catalog metadata checksum")

    @property
    def pin(self):
        return {"base_commit": self.base_commit, "image": self.image, "image_id": self.image_id}


@dataclass(frozen=True)
class Catalog:
    source_repository: str
    source_revision: str
    sha256: str
    tasks: tuple[TaskSpec, ...]

    def get(self, instance_id: str) -> TaskSpec:
        for task in self.tasks:
            if task.instance_id == instance_id:
                return task
        raise ValueError("task is outside the frozen catalog")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate catalog JSON field")
        result[key] = value
    return result


def load_catalog(path: str | Path, *, expected_sha256: str) -> Catalog:
    """Load the exact approved manifest; unknown fields and duplicate IDs fail closed.

    Schema v1: {schema_version, source:{repository,revision}, tasks:[{
      instance_id,repo,base_commit,image,image_id,metadata_sha256}]}.
    No problem statement, gold patch, tests or executable path is accepted here.
    """
    raw = Path(path).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if not _hex(expected_sha256, 64) or digest != expected_sha256:
        raise ValueError("catalog checksum differs from approved snapshot")
    data = json.loads(raw, object_pairs_hook=_unique_object)
    _fields(data, {"schema_version", "source", "tasks"}, "catalog")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise ValueError("unsupported catalog schema version")
    source = data["source"]
    _fields(source, {"repository", "revision"}, "catalog source")
    if source["repository"] != OFFICIAL_REPOSITORY or not _hex(source["revision"], 40):
        raise ValueError("invalid official catalog source revision")
    if not isinstance(data["tasks"], list) or not data["tasks"]:
        raise ValueError("catalog tasks must be a nonempty list")
    tasks = []
    seen = set()
    for row in data["tasks"]:
        _fields(row, {"instance_id", "repo", "base_commit", "image", "image_id", "metadata_sha256"}, "catalog task")
        if not _hex(row["metadata_sha256"], 64):
            raise ValueError("catalog task requires a metadata checksum")
        spec = TaskSpec(**row, source_repository=source["repository"], source_revision=source["revision"])
        if spec.instance_id in seen:
            raise ValueError("duplicate catalog task")
        seen.add(spec.instance_id)
        tasks.append(spec)
    return Catalog(source["repository"], source["revision"], digest, tuple(tasks))
