import pytest

from tartarus.process import ProcessError
from tartarus.shell import ShellError, resolve_minimal_shell_path


def test_resolve_minimal_shell_path_propagates_build_failure(monkeypatch):
    def fail(_command):
        raise ProcessError("cannot build `nixpkgs#coreutils`: nix not found")

    monkeypatch.setattr("tartarus.shell.run_checked", fail)

    with pytest.raises(ShellError, match="cannot build"):
        resolve_minimal_shell_path()


def test_resolve_minimal_shell_path_rejects_empty_output(monkeypatch):
    def empty(_command):
        return ""

    monkeypatch.setattr("tartarus.shell.run_checked", empty)

    with pytest.raises(ShellError, match="produced no store path"):
        resolve_minimal_shell_path()


def test_resolve_minimal_shell_path_rejects_no_bin_directory(monkeypatch, tmp_path):
    no_bin = tmp_path / "no-bin"
    no_bin.mkdir()

    def fake_build(_command):
        return f"{no_bin}\n"

    monkeypatch.setattr("tartarus.shell.run_checked", fake_build)

    with pytest.raises(ShellError, match="no output of"):
        resolve_minimal_shell_path()
