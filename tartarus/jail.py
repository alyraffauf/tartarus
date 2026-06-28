"""The JailBuilder: bwrap confinement for brokered commands (PLAN.md §6.7).

Builds a JailSpec from a capability grant and executes the command inside
bubblewrap. The jail binds only the declared shell and capability closures
read-only, mounts the work tree read-only, clears the host env, and sets PATH
explicitly. Writable grants re-bind only declared work-tree paths as writable;
package grants append package bin directories only for that one invocation.
Network grants route proxy-aware commands through a per-call filtering proxy;
plain raw-socket containment is a later namespace/firewall step. Unrestricted
grants skip bwrap entirely after policy approval, but still use the shell PATH.
"""

import os
import signal
import shlex
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from queue import Empty, Queue
from typing import Literal

from pydantic import ConfigDict, ValidationError, field_validator
from pydantic.dataclasses import dataclass as strict_dataclass

from tartarus.manifest import Grant
from tartarus.network_proxy import FilteringProxy

DEFAULT_JAIL_TIMEOUT_SECONDS = 30
TIMEOUT_EXIT_CODE = 124  # matches coreutils `timeout`

_STRICT = ConfigDict(frozen=True, extra="forbid", strict=True)


class JailError(Exception):
    """Raised when a jail cannot be built or run as requested."""


@strict_dataclass(config=_STRICT)
class ExecResult:
    code: int
    stdout: str
    stderr: str
    network_summary: str | None = None


@dataclass
class BackgroundHandle:
    """A detached jailed process, handed to the BackgroundRegistry.

    `proc` is the launched bwrap process (its own session leader, so `pgid`
    addresses the whole tree for signalling). `log_path` is the combined
    stdout+stderr sink the registry tails. `proxy`, when present, is the
    per-task filtering proxy whose lifetime the registry owns — it is stopped
    when the task exits or the harness shuts down.
    """

    proc: subprocess.Popen
    pgid: int
    log_path: str
    proxy: FilteringProxy | None = None


@strict_dataclass(config=_STRICT)
class JailSpec:
    work_tree: str
    shell_path: str
    base_env: dict[str, str]
    writable: list[str] = field(default_factory=list)
    extra_path: list[str] = field(default_factory=list)  # granted package bin dirs
    # The store paths bound read-only into the jail: the agent's baseline closure
    # (shell PATH + CA bundle) plus this grant's package closure. The jail sees
    # exactly these store paths and nothing else, so a capability reaches only its
    # declared closure (PLAN.md §13).
    bind_paths: list[str] = field(default_factory=list)
    allowed_hosts: list[str] = field(default_factory=list)
    network: Literal["none", "proxy"] = "none"
    unrestricted: bool = False

    @field_validator("writable")
    @classmethod
    def _validate_writable(cls, paths: list[str]) -> list[str]:
        # Belt-and-suspenders with Grant._validate_writable: JailBuilder.build
        # always feeds an already-validated Grant, but a JailSpec built directly
        # must still refuse anything that escapes the work tree.
        for path in paths:
            if path.startswith("/"):
                raise ValueError(f"writable path '{path}' must be relative")
            if ".." in path.split("/"):
                raise ValueError(f"writable path '{path}' escapes the work tree")
        return paths


class JailBuilder:
    def __init__(
        self,
        work_tree: str,
        shell_path: str,
        base_env: dict[str, str] | None = None,
        proxy_factory: Callable[[list[str]], FilteringProxy] | None = None,
        shell_closure: list[str] | None = None,
    ):
        self._work_tree = os.path.abspath(work_tree)
        self._shell_path = shell_path
        self._base_env = dict(base_env or {})
        self._bwrap_path = shutil.which("bwrap") or "bwrap"
        self._proxy_factory = proxy_factory or FilteringProxy
        # The baseline store paths every jailed call binds (shell PATH + CA
        # bundle closure), before this call's own grant closure is added.
        self._shell_closure = list(shell_closure or [])

    def build(self, grant: Grant) -> JailSpec:
        try:
            return JailSpec(
                work_tree=self._work_tree,
                shell_path=self._shell_path,
                base_env=self._base_env,
                writable=list(grant.writable),
                extra_path=list(grant.package_bins),
                bind_paths=_dedup(self._shell_closure + list(grant.closure_paths)),
                allowed_hosts=list(grant.allowed_hosts),
                network="proxy" if grant.allowed_hosts else "none",
                unrestricted=grant.unrestricted,
            )
        except ValidationError as error:
            raise JailError(str(error)) from error

    def exec(
        self,
        spec: JailSpec,
        command: str,
        timeout: int | None = DEFAULT_JAIL_TIMEOUT_SECONDS,
        output_callback: Callable[[str], None] | None = None,
        cancellation: threading.Event | None = None,
    ) -> ExecResult:
        if spec.unrestricted:
            return self._exec_unrestricted(
                spec, command, timeout, output_callback, cancellation
            )

        if spec.network == "proxy":
            with self._proxy_factory(spec.allowed_hosts) as proxy:
                result = self._exec_argv(
                    self._bwrap_argv(spec, command, proxy.url),
                    timeout,
                    output_callback,
                    cancellation,
                )
                return _append_stderr(result, proxy.summary())

        return self._exec_argv(
            self._bwrap_argv(spec, command), timeout, output_callback, cancellation
        )

    def exec_background(self, spec: JailSpec, command: str) -> BackgroundHandle:
        """Launch a jailed command detached and return immediately.

        Unlike `exec`, output is not captured into pipes (which would deadlock a
        long-lived task) — it is redirected to a per-task log file the registry
        tails. When the spec carries network grants, a filtering proxy is started
        here and handed to the caller, which owns stopping it when the task ends.
        """
        if spec.unrestricted:
            raise JailError("background execution does not support unrestricted grants")

        proxy: FilteringProxy | None = None
        proxy_url: str | None = None
        log_file: object | None = None

        try:
            if spec.network == "proxy":
                proxy = self._proxy_factory(spec.allowed_hosts)
                proxy.start()
                proxy_url = proxy.url

            argv = self._bwrap_argv(spec, command, proxy_url)
            log_path = self._background_log_path()
            log_file = open(log_path, "wb")
            try:
                proc = subprocess.Popen(
                    argv,
                    env={},
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,  # own session leader: pgid addresses the tree
                )
            finally:
                # The child holds its own dup of the fd; the parent's copy is not
                # needed once the process is spawned.
                log_file.close()

            return BackgroundHandle(
                proc=proc, pgid=os.getpgid(proc.pid), log_path=log_path, proxy=proxy
            )
        except FileNotFoundError as missing:
            if proxy is not None:
                proxy.stop()
            raise JailError(f"jail runtime not found: {missing}") from missing
        except OSError as error:
            if proxy is not None:
                proxy.stop()
            raise JailError(
                f"cannot open background log or run jail: {error}"
            ) from error

    def _background_log_path(self) -> str:
        bg_dir = os.path.join(self._work_tree, ".tartarus", "bg")
        os.makedirs(bg_dir, exist_ok=True)
        return os.path.join(bg_dir, f"{uuid.uuid4().hex}.log")

    def _exec_argv(
        self,
        argv: list[str],
        timeout: int | None,
        output_callback: Callable[[str], None] | None = None,
        cancellation: threading.Event | None = None,
    ) -> ExecResult:
        try:
            proc = subprocess.Popen(
                argv,
                env={},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,  # isolate process group for timeout kills
            )
        except FileNotFoundError as missing:
            raise JailError(f"jail runtime not found: {missing}") from missing

        return _wait_for_process(proc, timeout, output_callback, cancellation)

    def _exec_unrestricted(
        self,
        spec: JailSpec,
        command: str,
        timeout: int | None,
        output_callback: Callable[[str], None] | None = None,
        cancellation: threading.Event | None = None,
    ) -> ExecResult:
        env = {"PATH": self._compose_path(spec), **spec.base_env}
        try:
            proc = subprocess.Popen(
                shlex.split(command),
                cwd=spec.work_tree,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
        except FileNotFoundError as missing:
            raise JailError(
                f"unrestricted command runtime not found: {missing}"
            ) from missing

        return _wait_for_process(proc, timeout, output_callback, cancellation)

    def _bwrap_argv(
        self,
        spec: JailSpec,
        command: str,
        proxy_url: str | None = None,
    ) -> list[str]:
        env_args = ["--setenv", "PATH", self._compose_path(spec)]
        env_args += self._network_env_args(proxy_url)
        for key, value in spec.base_env.items():
            env_args += ["--setenv", key, value]

        return [
            self._bwrap_path,
            # Only this call's closure is visible, read-only — not the whole
            # store. bwrap synthesizes the /nix/store parent, so the in-jail
            # store contains exactly the bound closure (PLAN.md §13).
            *_store_bind_args(spec.bind_paths),
            *self._work_tree_bind_args(spec),
            *self._writable_bind_args(spec),
            "--chdir",
            "/work",
            "--unshare-all",  # new net/pid/ipc/uts/mount/cgroup namespaces
            *self._network_namespace_args(spec),
            "--die-with-parent",
            "--dir",
            "/proc",
            "--dev",
            "/dev",
            "--clearenv",  # start from empty env, then set PATH explicitly
            *env_args,
            "--",
            *shlex.split(command),
        ]

    @staticmethod
    def _network_namespace_args(spec: JailSpec) -> list[str]:
        if spec.network == "proxy":
            return ["--share-net"]
        return []

    @staticmethod
    def _network_env_args(proxy_url: str | None) -> list[str]:
        if proxy_url is None:
            return []
        return [
            "--setenv",
            "HTTP_PROXY",
            proxy_url,
            "--setenv",
            "HTTPS_PROXY",
            proxy_url,
            "--setenv",
            "ALL_PROXY",
            proxy_url,
            "--setenv",
            "http_proxy",
            proxy_url,
            "--setenv",
            "https_proxy",
            proxy_url,
            "--setenv",
            "all_proxy",
            proxy_url,
            "--setenv",
            "NO_PROXY",
            "",
            "--setenv",
            "no_proxy",
            "",
        ]

    @staticmethod
    def _work_tree_bind_args(spec: JailSpec) -> list[str]:
        if "." in spec.writable:
            return ["--bind", spec.work_tree, "/work"]
        return ["--ro-bind", spec.work_tree, "/work"]

    def _writable_bind_args(self, spec: JailSpec) -> list[str]:
        args = []
        for relative_path in spec.writable:
            if relative_path == ".":
                continue
            host_path = _host_writable_path(spec.work_tree, relative_path)
            jail_path = "/work" if relative_path == "." else f"/work/{relative_path}"
            args += ["--bind", host_path, jail_path]
        return args

    @staticmethod
    def _compose_path(spec: JailSpec) -> str:
        if not spec.extra_path:
            return spec.shell_path
        return ":".join([spec.shell_path, *spec.extra_path])


def _wait_for_process(
    proc: subprocess.Popen,
    timeout: int | None,
    output_callback: Callable[[str], None] | None,
    cancellation: threading.Event | None,
) -> ExecResult:
    output_queue: Queue[tuple[str, str | None]] = Queue()
    stdout_parts: list[str] = []
    stderr_parts: list[str] = []
    threads = [
        _start_pipe_reader(proc.stdout, "stdout", output_queue),
        _start_pipe_reader(proc.stderr, "stderr", output_queue),
    ]
    deadline = None if timeout is None else time.monotonic() + timeout
    termination_error: str | None = None

    while proc.poll() is None:
        _drain_output_queue(output_queue, stdout_parts, stderr_parts, output_callback)
        if cancellation is not None and cancellation.is_set():
            termination_error = "command cancelled"
            _terminate_process_group(proc)
            break
        if deadline is not None and time.monotonic() >= deadline:
            termination_error = f"command timed out after {timeout}s"
            _terminate_process_group(proc)
            break
        time.sleep(0.01)

    proc.wait()
    for thread in threads:
        thread.join()
    _drain_output_queue(output_queue, stdout_parts, stderr_parts, output_callback)

    stdout = "".join(stdout_parts)
    stderr = "".join(stderr_parts)
    if termination_error is not None:
        stderr = "\n".join(part for part in (stderr.strip(), termination_error) if part)
        if stderr:
            stderr += "\n"
        return ExecResult(TIMEOUT_EXIT_CODE, stdout, stderr)
    return ExecResult(proc.returncode, stdout, stderr)


def _start_pipe_reader(pipe, stream_name: str, output_queue: Queue):
    def read_lines() -> None:
        if pipe is None:
            output_queue.put((stream_name, None))
            return
        try:
            for line in pipe:
                output_queue.put((stream_name, line))
        finally:
            pipe.close()
            output_queue.put((stream_name, None))

    thread = threading.Thread(
        target=read_lines,
        name=f"tartarus-{stream_name}-reader",
        daemon=True,
    )
    thread.start()
    return thread


def _drain_output_queue(
    output_queue: Queue[tuple[str, str | None]],
    stdout_parts: list[str],
    stderr_parts: list[str],
    output_callback: Callable[[str], None] | None,
) -> None:
    while True:
        try:
            stream_name, text = output_queue.get_nowait()
        except Empty:
            return
        if text is None:
            continue
        if stream_name == "stdout":
            stdout_parts.append(text)
        else:
            stderr_parts.append(text)
        if output_callback is not None:
            output_callback(text)


def _terminate_process_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        # The process group may already be gone; fall through so we still reap
        # any stragglers below.
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def _append_stderr(result: ExecResult, message: str) -> ExecResult:
    stderr = "\n".join(part for part in (result.stderr.strip(), message) if part)
    if stderr:
        stderr += "\n"
    return ExecResult(result.code, result.stdout, stderr, network_summary=message)


def _store_bind_args(bind_paths: list[str]) -> list[str]:
    """`--ro-bind p p` for each closure path, replacing the whole-store mount."""
    args: list[str] = []
    for path in bind_paths:
        args += ["--ro-bind", path, path]
    return args


def _dedup(paths: list[str]) -> list[str]:
    """De-duplicate while preserving order (closures overlap on shared deps)."""
    seen: set[str] = set()
    unique: list[str] = []
    for path in paths:
        if path not in seen:
            seen.add(path)
            unique.append(path)
    return unique


def _host_writable_path(work_tree: str, relative_path: str) -> str:
    host_path = os.path.abspath(os.path.join(work_tree, relative_path))
    if host_path != work_tree and not host_path.startswith(work_tree + os.sep):
        raise JailError(f"writable path '{relative_path}' escapes the work tree")
    if not os.path.exists(host_path):
        os.makedirs(host_path, exist_ok=True)
    return host_path
