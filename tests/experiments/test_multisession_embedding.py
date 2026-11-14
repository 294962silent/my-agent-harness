"""Owned WSL GPU cleanup refuses unrelated or reused Linux process identities."""

import pytest
from experiments.memory_validation import multisession_embedding as target


def test_linux_paths_preserve_the_complete_windows_absolute_path():
    assert target.wsl_path("C:/Users/MLL/x/model") == "/mnt/c/Users/MLL/x/model"
    with pytest.raises(ValueError):
        target.wsl_path("relative/model")


@pytest.mark.parametrize(
    "actual", [None, {"pid": 9, "start": "wrong", "worker": True}, {"pid": 9, "start": "100", "worker": False}]
)
def test_cleanup_will_not_signal_a_reused_or_unrelated_pid(actual):
    assert not target.same_owned_process({"pid": 9, "start": "100", "worker": True}, actual)


def test_cleanup_identity_requires_matching_pid_start_time_and_worker_command():
    expected = {"pid": 9, "start": "100", "worker": True}
    assert target.same_owned_process(expected, dict(expected))
