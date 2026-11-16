"""Canonical source pins without changing historical raw-byte pins."""

import hashlib

import pytest

from my_coding_agent.rsi_branch import (
    CANONICAL_SOURCE_HASH_VERSION,
    CanonicalSourceError,
    canonical_source_bytes,
    canonical_source_sha256,
    file_sha,
    verify_canonical_source_pin,
)


def test_canonical_source_hash_matches_lf_crlf_and_lone_cr(tmp_path):
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    cr = tmp_path / "cr.py"
    lf.write_bytes(b"first = 1\nsecond = 2\n")
    crlf.write_bytes(b"first = 1\r\nsecond = 2\r\n")
    cr.write_bytes(b"first = 1\rsecond = 2\r")

    expected = hashlib.sha256(b"first = 1\nsecond = 2\n").hexdigest()
    assert CANONICAL_SOURCE_HASH_VERSION == "utf8-lf-v1"
    assert canonical_source_sha256(lf) == expected
    assert canonical_source_sha256(crlf) == expected
    assert canonical_source_sha256(cr) == expected
    assert verify_canonical_source_pin(crlf, expected) == expected

    # Historical pins intentionally remain exact-byte and are not migrated.
    assert file_sha(lf) == expected
    assert file_sha(crlf) != expected


def test_canonical_source_pin_rejects_content_changes(tmp_path):
    path = tmp_path / "source.py"
    path.write_bytes(b"value = 1\r\n")
    expected = canonical_source_sha256(path)
    path.write_bytes(b"value = 2\r\n")

    with pytest.raises(CanonicalSourceError, match="hash mismatch"):
        verify_canonical_source_pin(path, expected)


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (b"text\x00binary", "NUL"),
        (b"\xff\xfe", "valid UTF-8"),
    ],
)
def test_canonical_source_rejects_binary_and_invalid_utf8(payload, message):
    with pytest.raises(CanonicalSourceError, match=message):
        canonical_source_bytes(payload)


def test_canonical_source_pin_rejects_unknown_version_before_reading(tmp_path):
    with pytest.raises(CanonicalSourceError, match="unsupported"):
        canonical_source_sha256(tmp_path / "absent.py", version="future-v2")
