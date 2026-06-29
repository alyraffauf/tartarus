"""Integration tests for the bwrap jail (PLAN.md §11).

These require Linux with `bwrap` and `nix` present, so they skip elsewhere. They
prove the security invariants: confinement, content purity, and reach
isolation.
"""

import asyncio
import os
import shutil
import shlex
import subprocess
import sys

import tartarus.jail
import pytest

from tartarus.shell import ShellError, resolve_minimal_shell_path
from tartarus.jail import JailBuilder, JailError, JailSpec
from tartarus.manifest import Grant
from tests.helpers import HttpServer

_NEEDS_SANDBOX = pytest.mark.skipif(
    shutil.which("bwrap") is None or shutil.which("nix") is None,
    reason="requires bwrap and nix",
)

# Every jailed command runs under `bash -c` (JailBuilder._shell_argv), so `bash`
# must be resolvable on the spec's PATH. The unrestricted (host-escape) tests
# exec on the host, so they put the host bash dir on PATH; a real agent's
# unrestricted grant carries the shell PATH, which already includes bash.
_HOST_BASH_DIR = os.path.dirname(shutil.which("bash") or "")


def _exec(jail, *args, **kwargs):
    """Drive the async JailBuilder.exec to completion from a sync test."""
    return asyncio.run(jail.exec(*args, **kwargs))


def _store_root(path: str) -> str:
    """/nix/store/<hash-name>/anything -> /nix/store/<hash-name>."""
    return "/".join(path.split("/")[:4])


def _closure_of(store_paths: list[str]) -> list[str]:
    """The transitive closure of the given store paths' roots, via nix-store.

    The harness gets closures from Nix (`closureInfo`); these jail tests compute
    them independently so the bind set under test mirrors what Nix would emit.
    """
    roots = sorted({_store_root(path) for path in store_paths})
    out = subprocess.run(
        ["nix-store", "--query", "--requisites", *roots],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in out.stdout.splitlines() if line]


@pytest.fixture(scope="module")
def shell_path():
    return resolve_minimal_shell_path()


@pytest.fixture(scope="module")
def shell_closure(shell_path):
    """The baseline bind set: the closure of the minimal shell PATH."""
    return _closure_of(shell_path.split(":"))


@_NEEDS_SANDBOX
def test_echo_runs_confined(tmp_path, shell_path, shell_closure):
    jail = JailBuilder(str(tmp_path), shell_path, shell_closure=shell_closure)
    result = _exec(jail, jail.build(Grant()), "echo banana")

    assert result.code == 0
    assert "banana" in result.stdout


def test_bwrap_argv_wraps_command_with_shell_hook(tmp_path):
    jail = JailBuilder(str(tmp_path), "/nix/store/bash/bin")
    spec = JailSpec(
        work_tree=str(tmp_path),
        shell_path="/nix/store/bash/bin",
        base_env={"LC_ALL": "C.UTF-8"},
        shell_hook="/nix/store/hook",
    )

    argv = jail._bwrap_argv(spec, "echo hi")

    bash_env_idx = argv.index("BASH_ENV")
    assert argv[bash_env_idx + 1] == "/nix/store/hook"
    assert argv[-5:] == ["bash", "--noprofile", "--norc", "-c", "echo hi"]


def test_bwrap_argv_wraps_command_without_hook(tmp_path):
    jail = JailBuilder(str(tmp_path), "/nix/store/bash/bin")
    spec = JailSpec(
        work_tree=str(tmp_path),
        shell_path="/nix/store/bash/bin",
        base_env={"LC_ALL": "C.UTF-8"},
    )

    argv = jail._bwrap_argv(spec, "echo hi")

    assert "BASH_ENV" not in argv
    assert argv[-5:] == ["bash", "--noprofile", "--norc", "-c", "echo hi"]


@_NEEDS_SANDBOX
def test_shell_hook_runs_before_unrestricted_command(tmp_path, shell_path):
    hook = tmp_path / "hook"
    hook.write_text("export HOOK_FLAG=ran\n")
    jail = JailBuilder(str(tmp_path), shell_path)
    spec = JailSpec(
        work_tree=str(tmp_path),
        shell_path=shell_path,
        base_env={},
        shell_hook=str(hook),
        unrestricted=True,
    )

    result = _exec(jail, spec, "bash -c 'echo $HOOK_FLAG'")

    assert result.code == 0
    assert "ran" in result.stdout


@_NEEDS_SANDBOX
def test_bwrap_parent_environment_does_not_leak_into_proc(
    tmp_path, shell_path, shell_closure, monkeypatch
):
    monkeypatch.setenv("TARTARUS_TEST_HOST_SECRET", "secret-from-host-env")
    jail = JailBuilder(str(tmp_path), shell_path, shell_closure=shell_closure)

    result = _exec(jail, jail.build(Grant()), "cat /proc/1/environ")

    assert "TARTARUS_TEST_HOST_SECRET" not in result.stdout
    assert "secret-from-host-env" not in result.stdout
    assert "TARTARUS_TEST_HOST_SECRET" not in result.stderr
    assert "secret-from-host-env" not in result.stderr


@_NEEDS_SANDBOX
def test_proc_file_descriptors_are_not_available_for_output_injection(
    tmp_path, shell_path, shell_closure
):
    jail = JailBuilder(str(tmp_path), shell_path, shell_closure=shell_closure)

    result = _exec(
        jail,
        jail.build(Grant()),
        "bash -c 'echo injected-output > /proc/1/fd/1; echo normal-output'",
    )

    assert result.code == 0
    assert result.stdout == "normal-output\n"
    assert "injected-output" not in result.stdout


@_NEEDS_SANDBOX
def test_host_only_tool_is_absent_inside_jail(tmp_path, shell_path, shell_closure):
    jail = JailBuilder(str(tmp_path), shell_path, shell_closure=shell_closure)
    # git is not in the shell closure and was not granted, so it cannot resolve
    # by name even though the baseline shell binaries do.
    result = _exec(jail, jail.build(Grant()), "git --version")

    assert result.code != 0
    # bash resolves the bare name on PATH and finds nothing in the closure.
    assert "not found" in result.stderr.lower()


@_NEEDS_SANDBOX
def test_ungranted_tool_unreachable_by_absolute_store_path(
    tmp_path, shell_path, shell_closure
):
    # The store-bind purity gap (PLAN.md §13): with the whole store mounted, an
    # un-granted binary was reachable by absolute path. Now only the closure is
    # bound, so git's own store path does not exist inside the jail even though
    # its dependencies (bash, coreutils) are part of the shell closure.
    try:
        git_bin = resolve_minimal_shell_path(("nixpkgs#git",))
    except ShellError as error:
        pytest.skip(f"cannot resolve git package: {error}")

    jail = JailBuilder(str(tmp_path), shell_path, shell_closure=shell_closure)
    result = _exec(jail, jail.build(Grant()), f"{shlex.quote(git_bin)}/git --version")

    assert result.code != 0
    assert "no such file" in result.stderr.lower()


@_NEEDS_SANDBOX
def test_no_host_filesystem_beyond_work_tree(tmp_path, shell_path, shell_closure):
    jail = JailBuilder(str(tmp_path), shell_path, shell_closure=shell_closure)
    result = _exec(jail, jail.build(Grant()), "ls /")

    visible = set(result.stdout.split())
    assert visible <= {"dev", "nix", "proc", "work"}
    assert "home" not in visible
    assert "usr" not in visible


@_NEEDS_SANDBOX
def test_no_network_interfaces_inside_jail(tmp_path, shell_path, shell_closure):
    jail = JailBuilder(str(tmp_path), shell_path, shell_closure=shell_closure)
    # --unshare-all removed the network namespace, so /sys/class/net is gone.
    result = _exec(jail, jail.build(Grant()), "ls /sys/class/net")

    assert result.code != 0


def test_network_grant_builds_proxy_spec(tmp_path):
    jail = JailBuilder(str(tmp_path), _HOST_BASH_DIR)
    spec = jail.build(Grant(allowed_hosts=["example.com:443"]))

    assert spec.network == "proxy"
    assert spec.allowed_hosts == ["example.com:443"]


def test_proxy_jail_sets_proxy_environment(tmp_path):
    jail = JailBuilder(str(tmp_path), _HOST_BASH_DIR)
    spec = jail.build(Grant(allowed_hosts=["example.com:443"]))

    argv = jail._bwrap_argv(spec, "true", proxy_url="http://127.0.0.1:12345")

    assert "--share-net" in argv
    assert "HTTP_PROXY" in argv
    assert "http://127.0.0.1:12345" in argv


@_NEEDS_SANDBOX
def test_proxy_jail_routes_curl_through_allowed_host(
    tmp_path, shell_path, shell_closure
):
    with HttpServer() as upstream:
        upstream_host, upstream_port = upstream.server_address
        curl_bins = _curl_bin_dirs()
        jail = JailBuilder(
            str(tmp_path),
            shell_path,
            shell_closure=shell_closure,
        )
        spec = jail.build(
            Grant(
                package_bins=curl_bins,
                closure_paths=_closure_of(curl_bins),
                allowed_hosts=[f"{upstream_host}:{upstream_port}"],
            )
        )

        result = _exec(jail, spec, f"curl -fsS http://{upstream_host}:{upstream_port}/")

        assert result.code == 0
        assert result.stdout == "hello"
        assert "proxy decisions: 1 allowed, 0 blocked" in result.stderr
        assert result.network_summary is not None
        assert "1 allowed, 0 blocked" in result.network_summary


@_NEEDS_SANDBOX
def test_proxy_jail_blocks_unlisted_host(tmp_path, shell_path, shell_closure):
    with HttpServer() as upstream:
        upstream_host, upstream_port = upstream.server_address
        curl_bins = _curl_bin_dirs()
        jail = JailBuilder(
            str(tmp_path),
            shell_path,
            shell_closure=shell_closure,
        )
        spec = jail.build(
            Grant(
                package_bins=curl_bins,
                closure_paths=_closure_of(curl_bins),
                allowed_hosts=["example.com:80"],
            )
        )

        result = _exec(jail, spec, f"curl -fsS http://{upstream_host}:{upstream_port}/")

        assert result.code != 0
        assert "proxy decisions: 0 allowed, 1 blocked" in result.stderr
        assert result.network_summary is not None
        assert "0 allowed, 1 blocked" in result.network_summary


@_NEEDS_SANDBOX
def test_writable_grant_allows_only_declared_path(tmp_path, shell_path, shell_closure):
    writable_dir = tmp_path / "allowed"
    readonly_dir = tmp_path / "readonly"
    writable_dir.mkdir()
    readonly_dir.mkdir()

    jail = JailBuilder(str(tmp_path), shell_path, shell_closure=shell_closure)
    spec = jail.build(Grant(writable=["allowed"]))

    allowed = _exec(jail, spec, "bash -c 'echo yes > allowed/file.txt'")
    denied = _exec(jail, spec, "bash -c 'echo no > readonly/file.txt'")

    assert allowed.code == 0
    assert (writable_dir / "file.txt").read_text().strip() == "yes"
    assert denied.code != 0
    assert not (readonly_dir / "file.txt").exists()


def test_package_bins_add_extra_path_for_one_spec(tmp_path):
    jail = JailBuilder(str(tmp_path), "/shell/bin")

    spec = jail.build(Grant(package_bins=["/nix/store/jq/bin"]))
    baseline = jail.build(Grant())

    assert spec.extra_path == ["/nix/store/jq/bin"]
    assert baseline.extra_path == []


def test_build_unions_shell_and_grant_closure(tmp_path):
    jail = JailBuilder(
        str(tmp_path),
        "/shell/bin",
        shell_closure=["/nix/store/bash", "/nix/store/coreutils"],
    )

    spec = jail.build(Grant(closure_paths=["/nix/store/coreutils", "/nix/store/jq"]))
    baseline = jail.build(Grant())

    # Baseline carries the shell closure; the grant adds its own, de-duplicated.
    assert baseline.bind_paths == ["/nix/store/bash", "/nix/store/coreutils"]
    assert spec.bind_paths == [
        "/nix/store/bash",
        "/nix/store/coreutils",
        "/nix/store/jq",
    ]


def test_bwrap_argv_binds_each_closure_path_not_whole_store(tmp_path):
    jail = JailBuilder(str(tmp_path), "/shell/bin", shell_closure=["/nix/store/bash"])
    spec = jail.build(Grant(closure_paths=["/nix/store/jq"]))

    argv = jail._bwrap_argv(spec, "true")

    # Each closure path is bound individually; the whole store is never mounted.
    assert _ro_bind_pairs(argv) >= {
        ("/nix/store/bash", "/nix/store/bash"),
        ("/nix/store/jq", "/nix/store/jq"),
    }
    assert ("/nix/store", "/nix/store") not in _ro_bind_pairs(argv)


def _ro_bind_pairs(argv: list[str]) -> set[tuple[str, str]]:
    pairs = set()
    for index, token in enumerate(argv):
        if token == "--ro-bind":
            pairs.add((argv[index + 1], argv[index + 2]))
    return pairs


def test_unrestricted_grant_bypasses_bwrap_after_approval_path(tmp_path):
    work_tree = tmp_path / "work"
    work_tree.mkdir()
    secret = tmp_path / "secret.txt"
    secret.write_text("outside work tree")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    reader = bin_dir / "read-secret"
    reader.write_text(
        "#!/bin/sh\nIFS= read -r line < ../secret.txt || true\nprintf '%s' \"$line\"\n"
    )
    reader.chmod(0o755)

    jail = JailBuilder(str(work_tree), os.pathsep.join([str(bin_dir), _HOST_BASH_DIR]))
    spec = jail.build(Grant(unrestricted=True))

    result = _exec(jail, spec, "read-secret")

    assert result.code == 0
    assert result.stdout == "outside work tree"


def test_exec_streams_unrestricted_output_lines(tmp_path):
    jail = JailBuilder(str(tmp_path), _HOST_BASH_DIR)
    spec = jail.build(Grant(unrestricted=True))
    lines: list[str] = []
    code = "import sys; print('one', flush=True); print('two', flush=True)"

    result = _exec(
        jail,
        spec,
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
        output_callback=lines.append,
    )

    assert result.code == 0
    assert result.stdout == "one\ntwo\n"
    assert lines == ["one\n", "two\n"]


def test_exec_streams_unrestricted_output_without_newlines(tmp_path):
    jail = JailBuilder(str(tmp_path), _HOST_BASH_DIR)
    spec = jail.build(Grant(unrestricted=True))
    lines: list[str] = []
    payload = ("x" * 70_000) + "é"
    code = f"import sys; sys.stdout.write({payload!r}); sys.stdout.flush()"

    result = _exec(
        jail,
        spec,
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}",
        output_callback=lines.append,
    )

    assert result.code == 0
    assert result.stdout == payload
    assert "".join(lines) == payload


_SLOW_PROGRAM = (
    "import time; "
    "print('started', flush=True); "
    "time.sleep(10); "
    "print('finished', flush=True)"
)


def test_exec_timeout_kills_unrestricted_process(tmp_path):
    jail = JailBuilder(str(tmp_path), _HOST_BASH_DIR)
    spec = jail.build(Grant(unrestricted=True))

    result = _exec(
        jail,
        spec,
        f"{shlex.quote(sys.executable)} -c {shlex.quote(_SLOW_PROGRAM)}",
        timeout=1,
    )

    assert result.code == 124
    assert result.stdout == "started\n"
    assert "timed out" in result.stderr


def test_exec_cancellation_terminates_unrestricted_process(tmp_path):
    jail = JailBuilder(str(tmp_path), _HOST_BASH_DIR)
    spec = jail.build(Grant(unrestricted=True))
    lines: list[str] = []
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(_SLOW_PROGRAM)}"

    async def run_and_cancel():
        task = asyncio.create_task(
            jail.exec(spec, command, output_callback=lines.append)
        )
        while not lines and not task.done():
            await asyncio.sleep(0.01)
        if task.done():
            await task  # surface a launch failure instead of spinning forever
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run_and_cancel())

    # The process was killed during its sleep, so its later output never arrives.
    assert lines == ["started\n"]


def _curl_bin_dirs() -> list[str]:
    try:
        return [resolve_minimal_shell_path(("nixpkgs#curl",))]
    except ShellError as error:
        pytest.skip(f"cannot resolve curl package: {error}")


def test_exec_background_stops_proxy_on_popen_failure(tmp_path, monkeypatch):
    """A network-enabled background task must clean up its proxy even if Popen fails."""

    class FakeProxy:
        def __init__(self, allowed_hosts):
            self.allowed_hosts = allowed_hosts
            self.stopped = False

        def start(self) -> None:
            pass

        @property
        def url(self) -> str:
            return "http://127.0.0.1:9999"

        def stop(self) -> None:
            self.stopped = True

        def summary(self) -> str:
            return "fake summary"

    captured: dict[str, FakeProxy] = {}

    def factory(allowed_hosts):
        proxy = FakeProxy(allowed_hosts)
        captured["proxy"] = proxy
        return proxy

    jail = JailBuilder(str(tmp_path), "/bin/sh", proxy_factory=factory)
    spec = jail.build(Grant(allowed_hosts=["example.com:80"]))

    def raising_popen(*_args, **_kwargs):
        raise OSError("popen failed")

    monkeypatch.setattr(tartarus.jail.subprocess, "Popen", raising_popen)

    with pytest.raises(JailError):
        jail.exec_background(spec, "true")

    assert captured["proxy"].stopped is True
