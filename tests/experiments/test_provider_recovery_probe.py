"""Safety contracts for the non-scoring provider recovery gate."""

from __future__ import annotations

from experiments.long_horizon_quality.provider_recovery_probe import _safe_error


def test_provider_error_record_redacts_likely_credentials() -> None:
    value = _safe_error(
        RuntimeError(
            "Authorization: Bearer super-secret-token\n"
            "api_key=another-secret\n"
            "upstream returned a timeout"
        )
    )

    assert value["type"] == "RuntimeError"
    assert "super-secret-token" not in value["message"]
    assert "another-secret" not in value["message"]
    assert "upstream returned a timeout" in value["message"]
