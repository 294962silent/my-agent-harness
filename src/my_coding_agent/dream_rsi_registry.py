"""Trusted global pure-parameter registry; immutable versions and atomic pointers."""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from .dream_rsi_contract import SOURCE_COMMIT, canonical, checksum, policy_identity, validate_parameters, _unique_object


class PolicyRegistry:
    def __init__(self, root=None):
        if root is None:
            from .paths import AgentPaths

            root = AgentPaths().home / "dream-rsi"
        self.root = Path(root).resolve()

    @contextmanager
    def _lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".lock").open("a+b") as stream:
            stream.seek(0, 2)
            if stream.tell() == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            acquired = False
            try:
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                yield
            except OSError as error:
                if not acquired:
                    raise RuntimeError("policy registry is busy") from error
                raise
            finally:
                if acquired:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _parse_pointers(raw):
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            not isinstance(value, dict)
            or set(value)
            not in ({"schema_version", "active", "previous"}, {"schema_version", "active", "previous", "revision"})
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or any(
                value[k] is not None and (not isinstance(value[k], str) or not re.fullmatch(r"[0-9a-f]{64}", value[k]))
                for k in ("active", "previous")
            )
        ):
            raise ValueError("invalid policy registry pointers")
        if "revision" in value and (
            not isinstance(value["revision"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["revision"])
        ):
            raise ValueError("invalid registry revision")
        return value

    def _pointers(self):
        try:
            return self._parse_pointers((self.root / "pointers.json").read_bytes())
        except FileNotFoundError:
            return {"schema_version": 1, "active": None, "previous": None}

    def _version(self, version):
        if not isinstance(version, str) or not re.fullmatch(r"[0-9a-f]{64}", version):
            raise ValueError("invalid policy version")
        try:
            raw = (self.root / "versions" / (version + ".json")).read_bytes()
        except OSError:
            raise ValueError("policy version is unavailable") from None
        if checksum(raw) != version:
            raise ValueError("immutable policy version checksum differs")
        value = json.loads(raw, object_pairs_hook=_unique_object)
        if (
            not isinstance(value, dict)
            or set(value) != {"schema_version", "source_commit", "parameters", "policy_hash", "receipt"}
            or type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["source_commit"] != SOURCE_COMMIT
            or value["policy_hash"] != policy_identity(value["parameters"])
        ):
            raise ValueError("invalid parameter policy version")
        validate_parameters(value["parameters"])
        if (
            not isinstance(value["receipt"], dict)
            or set(value["receipt"]) != {"evidence_sha256", "plan_sha256", "summary_sha256"}
            or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-f]{64}", v) for v in value["receipt"].values())
        ):
            raise ValueError("invalid promotion receipt")
        return value

    def snapshot(self):
        """One pointer read supplies the active policy and its ABA-safe CAS token."""
        try:
            raw_pointer = (self.root / "pointers.json").read_bytes()
        except FileNotFoundError:
            raw_pointer = None
        except OSError:
            raise ValueError("registry state cannot be frozen") from None
        token = "absent" if raw_pointer is None else checksum(raw_pointer)
        try:
            pointers = (
                self._parse_pointers(raw_pointer) if raw_pointer is not None else {"active": None, "previous": None}
            )
            if pointers["active"] is None:
                return {
                    "parameters": None,
                    "version": None,
                    "reason": "no_active_policy",
                    "previous": pointers["previous"],
                    "token": token,
                    "raw_document": None,
                }
            value = self._version(pointers["active"])
            raw_document = (self.root / "versions" / (pointers["active"] + ".json")).read_bytes()
            if checksum(raw_document) != pointers["active"]:
                raise ValueError("immutable version changed during snapshot")
            return {
                "parameters": value["parameters"],
                "policy_hash": value["policy_hash"],
                "version": pointers["active"],
                "previous": pointers["previous"],
                "reason": "registry_active",
                "token": token,
                "raw_document": raw_document,
            }
        except (OSError, ValueError, TypeError, KeyError):
            return {
                "parameters": None,
                "version": None,
                "reason": "invalid_registry",
                "previous": None,
                "token": token,
                "raw_document": None,
            }

    def status(self):
        try:
            return {k: v for k, v in self.snapshot().items() if k != "raw_document"}
        except ValueError:
            return {"parameters": None, "version": None, "reason": "invalid_registry", "previous": None}

    def _replace(self, pointers):
        temporary = self.root / (".pointers-" + uuid4().hex + ".json")
        try:
            with temporary.open("xb") as stream:
                stream.write(canonical({**pointers, "revision": uuid4().hex}))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / "pointers.json")
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _document(proof):
        validate_parameters(proof["parameters"])
        return {
            "schema_version": 1,
            "source_commit": SOURCE_COMMIT,
            **{k: proof[k] for k in ("parameters", "policy_hash", "receipt")},
        }

    def _check_incumbent(self, proof):
        current = self.snapshot()
        if (
            Path(proof["registry_root"]).resolve() != self.root
            or current["token"] != proof["expected_token"]
            or current["version"] != proof["expected_incumbent_version"]
            or policy_identity(current["parameters"]) != proof["expected_incumbent_hash"]
        ):
            raise ValueError("stale optimizer incumbent; registry changed since plan freeze")
        return current

    def prepare(self, run):
        """Predict the immutable target, without changing any registry state."""
        from .dream_rsi_optimizer import verify_promotion

        proof = verify_promotion(run)
        self._check_incumbent(proof)
        return checksum(canonical(self._document(proof)))

    def promote(self, run, *, deadline=None, expected_version=None):
        from .dream_rsi_optimizer import verify_promotion

        with self._lock():
            proof = verify_promotion(run)
            current = self._check_incumbent(proof)
            value = self._document(proof)
            raw = canonical(value)
            version = checksum(raw)
            if expected_version is not None and version != expected_version:
                raise ValueError("prepared policy version no longer matches verified evidence")
            directory = self.root / "versions"
            directory.mkdir(exist_ok=True)
            path = directory / (version + ".json")
            if path.exists():
                self._version(version)
            else:
                with path.open("xb") as stream:
                    stream.write(raw)
                    stream.flush()
                    os.fsync(stream.fileno())
            self._version(version)
            if current["version"] != version:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("parameter search global deadline exceeded before pointer update")
                self._check_incumbent(proof)
                self._replace({"schema_version": 1, "active": version, "previous": current["version"]})
            return {"promoted": True, "version": version, "reason": "observed_strict_validation_gain"}

    def rollback(self, version=None):
        with self._lock():
            pointers = self._pointers()
            target = version or pointers["previous"]
            if target is None:
                raise ValueError("no previous policy to rollback")
            self._version(target)
            old = pointers["active"]
            if old is not None:
                try:
                    self._version(old)
                except (OSError, ValueError, TypeError, KeyError):
                    old = None  # A valid previous version can recover a corrupt active one.
            self._replace({"schema_version": 1, "active": target, "previous": old})
            return self.status()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["status", "rollback"])
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--version")
    args = parser.parse_args(argv)
    try:
        from .settings import load_settings

        config = getattr(load_settings(cwd=None), "dream_optimization", None)
        registry = PolicyRegistry(args.registry or getattr(config, "registry_path", None))
        result = registry.status() if args.action == "status" else registry.rollback(args.version)
        result.update(
            optimization_enabled=getattr(config, "enabled", False),
            optimization_status=getattr(config, "status", "disabled"),
            active=result["parameters"] is not None,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(json.dumps({"status": "failed", "error_type": type(error).__name__}))
        return 1
    print(json.dumps(result))
    return 0
