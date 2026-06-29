"""Minimal shell-path helper for tests and ad-hoc store resolution.

The harness no longer resolves a live shell: an agent's baseline PATH is baked
into its bundle manifest (`shellPath`) at build time. What remains
here is `resolve_minimal_shell_path`, used by the jail integration tests to build
a small PATH from named packages.
"""

import os

from tartarus.process import ProcessError, run_checked

DEFAULT_SHELL_PACKAGES = ("nixpkgs#coreutils", "nixpkgs#bash")


class ShellError(Exception):
    """Raised when a shell package cannot be resolved to a bin directory."""


def resolve_minimal_shell_path(
    packages: tuple[str, ...] = DEFAULT_SHELL_PACKAGES,
) -> str:
    """Realize each package into the store and join their `bin` dirs into a PATH."""
    return ":".join(_resolve_bin_dir(package) for package in packages)


def _resolve_bin_dir(package: str) -> str:
    store_paths = _build_package(package)
    if not store_paths:
        raise ShellError(f"`nix build {package}` produced no store path")

    # A package may have several outputs (out, man, dev, ...); pick the one that
    # actually carries executables.
    for store_path in store_paths:
        bin_dir = os.path.join(store_path, "bin")
        if os.path.isdir(bin_dir):
            return bin_dir
    raise ShellError(f"no output of `{package}` has a bin directory")


def _build_package(package: str) -> list[str]:
    command = ["nix", "build", package, "--no-link", "--print-out-paths"]
    try:
        stdout = run_checked(command)
    except ProcessError as error:
        raise ShellError(f"cannot build `{package}`: {error}") from error

    return [line for line in stdout.splitlines() if line.strip()]
