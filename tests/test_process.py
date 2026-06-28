import pytest

from tartarus.process import ProcessError, run_checked


def test_run_checked_returns_stdout():
    assert run_checked(["echo", "-n", "hello"]) == "hello"


def test_run_checked_raises_on_empty_command():
    with pytest.raises(ProcessError, match="empty"):
        run_checked([])


def test_run_checked_raises_on_missing_binary():
    with pytest.raises(ProcessError, match="not found"):
        run_checked(["nonexistent_command_xyz"])


def test_run_checked_raises_on_nonzero_exit():
    with pytest.raises(ProcessError, match="failed"):
        run_checked(["sh", "-c", "exit 1"])
