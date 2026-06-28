import asyncio

import pytest

from tartarus.audit import AuditEvent
from tartarus.background import BackgroundError, BackgroundRegistry
from tartarus.broker import Broker, interpolate, validate_args
from tartarus.jail import ExecResult, JailBuilder, JailError, JailSpec
from tartarus.manifest import (
    Capability,
    Grant,
    Param,
    build_manifest,
)
from tartarus.models import ToolCall
from tartarus.policy import PolicyEngine
from tests.manifest_fixtures import echo_manifest


class FakeJail(JailBuilder):
    """Records what it is asked to build/exec so broker logic can be tested without
    bwrap. Real confinement is covered by tests/test_jail.py."""

    def __init__(
        self, result: ExecResult | None = None, error: JailError | None = None
    ):
        super().__init__(work_tree="", shell_path="", base_env={})
        self._result = result if result is not None else ExecResult(0, "", "")
        self._error = error
        self.built_grants: list[Grant] = []
        self.exec_commands: list[str] = []
        self.exec_timeouts: list[int | None] = []
        self.background_commands: list[str] = []

    def build(self, grant: Grant) -> JailSpec:
        self.built_grants.append(grant)
        return JailSpec(work_tree="", shell_path="", base_env={})

    async def exec(
        self,
        spec: JailSpec,
        command: str,
        timeout: int | None = 0,
        output_callback=None,
    ) -> ExecResult:
        self.exec_commands.append(command)
        self.exec_timeouts.append(timeout)
        if self._error is not None:
            raise self._error
        if output_callback is not None and self._result.stdout:
            output_callback(self._result.stdout)
        return self._result

    def exec_background(self, spec: JailSpec, command: str):
        self.background_commands.append(command)
        if self._error is not None:
            raise self._error
        return object()  # opaque handle; the broker hands it to the registry


class FakeRegistry(BackgroundRegistry):
    """Records launches and answers control ops without real processes."""

    def __init__(self):
        super().__init__()
        self.registered: list[tuple[str, object]] = []
        self.calls: list[tuple] = []

    def register(self, capability, handle) -> str:
        self.registered.append((capability, handle))
        return f"bg-{len(self.registered)}"

    def status(self, task_id: str | None) -> str:
        self.calls.append(("status", task_id))
        return f"status {task_id}"

    def output(self, task_id: str | None, offset: int = 0) -> str:
        self.calls.append(("output", task_id, offset))
        return f"output {task_id} from {offset}"

    def stop(self, task_id: str | None) -> str:
        self.calls.append(("stop", task_id))
        return f"stopped {task_id}"


class MemoryAuditLog:
    def __init__(self):
        self.events: list[AuditEvent] = []

    def record(self, event: AuditEvent) -> None:
        self.events.append(event)


def _call(name, arguments, argument_error=None):
    return ToolCall(
        id="call-1", name=name, arguments=arguments, argument_error=argument_error
    )


def _handle(broker, call):
    """Drive the async broker to completion from a sync test."""
    return asyncio.run(broker.handle(call))


def _broker(jail=None, policy=None, audit=None):
    return Broker(
        echo_manifest(),
        jail if jail is not None else FakeJail(),
        policy if policy is not None else PolicyEngine(),
        audit=audit,
    )


def test_echo_round_trips_through_jail():
    jail = FakeJail(result=ExecResult(0, "hello world", ""))
    broker = _broker(jail)

    result = _handle(broker, _call("echo", {"message": "hello world"}))

    assert not result.is_error
    assert result.output == "hello world"
    assert jail.exec_commands == ["echo 'hello world'"]


def test_allowed_call_records_one_audit_event():
    audit = MemoryAuditLog()
    jail = FakeJail(result=ExecResult(0, "hello world", ""))
    broker = _broker(jail, audit=audit)

    _handle(broker, _call("echo", {"message": "hello world"}))

    assert len(audit.events) == 1
    event = audit.events[0]
    assert event.tool_name == "echo"
    assert event.capability is not None
    assert event.capability.name == "echo"
    assert event.command == "echo 'hello world'"
    assert event.decision is not None
    assert event.decision.allowed is True
    assert event.exec_result is not None
    assert event.exec_result.code == 0
    assert event.result.output == "hello world"


def test_nonzero_exit_is_reported_as_error():
    jail = FakeJail(result=ExecResult(1, "", "boom"))
    broker = _broker(jail)

    result = _handle(broker, _call("echo", {"message": "x"}))

    assert result.is_error
    assert "boom" in result.output


def test_undeclared_timeout_runs_unbounded():
    # A capability without a declared timeout runs with no ceiling: the jail
    # receives None, which the process wait loop treats as "wait forever".
    jail = FakeJail(result=ExecResult(0, "hi", ""))
    broker = _broker(jail)

    _handle(broker, _call("echo", {"message": "x"}))

    assert jail.exec_timeouts == [None]


def test_declared_timeout_reaches_the_jail():
    capability = Capability(
        name="run_tests",
        description="run",
        policy="auto",
        params={},
        grants=Grant(),
        runner="pytest",
        timeout=300,
    )
    jail = FakeJail(result=ExecResult(0, "ok", ""))
    broker = Broker(build_manifest({"run_tests": capability}), jail, PolicyEngine())

    _handle(broker, _call("run_tests", {}))

    assert jail.exec_timeouts == [300]


def test_output_truncate_is_configurable():
    jail = FakeJail(result=ExecResult(0, "abcdef", ""))
    broker = Broker(echo_manifest(), jail, PolicyEngine(), output_truncate=3)

    result = _handle(broker, _call("echo", {"message": "x"}))

    assert result.output == "abc\n...(truncated)"


def test_jail_error_is_surfaced_not_raised():
    audit = MemoryAuditLog()
    jail = FakeJail(error=JailError("network grants not implemented"))
    broker = _broker(jail, audit=audit)

    result = _handle(broker, _call("echo", {"message": "x"}))

    assert result.is_error
    assert "jail error" in result.output
    assert len(audit.events) == 1
    assert audit.events[0].broker_error == "jail error: network grants not implemented"
    assert audit.events[0].exec_result is None


def test_unknown_tool_never_reaches_the_jail():
    audit = MemoryAuditLog()
    jail = FakeJail()
    broker = _broker(jail, audit=audit)

    result = _handle(broker, _call("nope", {}))

    assert result.is_error
    assert "unknown tool" in result.output
    assert jail.exec_commands == []
    assert len(audit.events) == 1
    assert audit.events[0].capability is None
    assert audit.events[0].decision is not None
    assert audit.events[0].decision.reason == "unknown tool"


def test_malformed_arguments_never_reach_the_jail():
    audit = MemoryAuditLog()
    jail = FakeJail()
    broker = _broker(jail, audit=audit)

    result = _handle(broker, _call("echo", {}, argument_error="bad JSON"))

    assert result.is_error
    assert "invalid arguments" in result.output
    assert jail.exec_commands == []
    assert len(audit.events) == 1
    assert audit.events[0].broker_error == "invalid arguments: bad JSON"


def test_missing_required_argument_never_reaches_the_jail():
    jail = FakeJail()
    broker = _broker(jail)

    result = _handle(broker, _call("echo", {}))

    assert result.is_error
    assert "missing required parameter 'message'" in result.output
    assert jail.exec_commands == []


def test_deny_capability_never_reaches_the_jail():
    jail = FakeJail()
    capabilities = {
        "locked": Capability(
            name="locked",
            description="no",
            policy="deny",
            params={},
            grants=Grant(),
            runner="true",
        )
    }
    broker = Broker(build_manifest(capabilities), jail, PolicyEngine())

    result = _handle(broker, _call("locked", {}))

    assert result.is_error
    assert "denied" in result.output
    assert jail.exec_commands == []


def _ask_always_manifest():
    capability = Capability(
        name="run_command",
        description="run",
        policy="ask-always",
        params={"command": Param(type="string", description="", required=True)},
        grants=Grant(),
        runner="bash -c {command}",
    )
    return build_manifest({"run_command": capability})


def _unrestricted_manifest():
    capability = Capability(
        name="shell_escape",
        description="escape",
        policy="ask-always",
        params={"command": Param(type="string", description="", required=True)},
        grants=Grant(unrestricted=True),
        runner="bash -c {command}",
    )
    return build_manifest({"shell_escape": capability})


def test_ask_always_decline_denies_and_skips_the_jail():
    audit = MemoryAuditLog()
    jail = FakeJail(result=ExecResult(0, "ran", ""))
    broker = Broker(
        _ask_always_manifest(),
        jail,
        PolicyEngine(prompt=lambda *_: False),
        audit=audit,
    )

    result = _handle(broker, _call("run_command", {"command": "ls"}))

    assert result.is_error
    assert "denied" in result.output
    assert jail.exec_commands == []
    assert len(audit.events) == 1
    assert audit.events[0].decision is not None
    assert audit.events[0].decision.reason == "declined by human"
    assert audit.events[0].command == "bash -c ls"


def test_ask_always_approval_runs_command_in_jail():
    jail = FakeJail(result=ExecResult(0, "ran", ""))
    broker = Broker(
        _ask_always_manifest(),
        jail,
        PolicyEngine(prompt=lambda *_: True),
    )

    result = _handle(broker, _call("run_command", {"command": "ls -la"}))

    assert not result.is_error
    # The command is a single shell-escaped argument to bash -c.
    assert jail.exec_commands == ["bash -c 'ls -la'"]


def test_unrestricted_decline_denies_and_skips_the_jail():
    jail = FakeJail(result=ExecResult(0, "ran", ""))
    broker = Broker(
        _unrestricted_manifest(),
        jail,
        PolicyEngine(prompt=lambda *_: False),
    )

    result = _handle(broker, _call("shell_escape", {"command": "cat /etc/passwd"}))

    assert result.is_error
    assert "denied" in result.output
    assert jail.exec_commands == []


def test_unrestricted_approval_reaches_the_jail_builder():
    jail = FakeJail(result=ExecResult(0, "ran", ""))
    broker = Broker(
        _unrestricted_manifest(),
        jail,
        PolicyEngine(prompt=lambda *_: True),
    )

    result = _handle(broker, _call("shell_escape", {"command": "cat /etc/passwd"}))

    assert not result.is_error
    assert jail.built_grants == [Grant(unrestricted=True)]
    assert jail.exec_commands == ["bash -c 'cat /etc/passwd'"]


def _background_manifest():
    capability = Capability(
        name="run_bg",
        description="run detached",
        policy="ask-always",
        params={"command": Param(type="string", description="", required=True)},
        grants=Grant(writable=["."]),
        runner="bash -c {command}",
        kind="background",
    )
    return build_manifest({"run_bg": capability})


def _control_manifest(name, op, policy="auto"):
    capability = Capability(
        name=name,
        description="control",
        policy=policy,
        params={"task": Param(type="string", description="", required=True)},
        grants=Grant(),
        runner="",
        kind="control",
        control=op,
    )
    return build_manifest({name: capability})


def test_background_capability_launches_and_registers():
    jail = FakeJail()
    registry = FakeRegistry()
    broker = Broker(
        _background_manifest(),
        jail,
        PolicyEngine(prompt=lambda *_: True),
        registry=registry,
    )

    result = _handle(broker, _call("run_bg", {"command": "sleep 1"}))

    assert not result.is_error
    assert result.output == "started bg-1"
    assert jail.background_commands == ["bash -c 'sleep 1'"]
    assert jail.exec_commands == []  # the synchronous exec path is untouched
    assert registry.registered == [("run_bg", registry.registered[0][1])]


def test_background_without_registry_reports_unavailable():
    jail = FakeJail()
    broker = Broker(_background_manifest(), jail, PolicyEngine(prompt=lambda *_: True))

    result = _handle(broker, _call("run_bg", {"command": "sleep 1"}))

    assert result.is_error
    assert "not available" in result.output
    assert jail.background_commands == []


def test_control_capability_dispatches_to_registry_not_jail():
    jail = FakeJail()
    registry = FakeRegistry()
    broker = Broker(
        _control_manifest("bg_output", "output"),
        jail,
        PolicyEngine(),
        registry=registry,
    )

    result = _handle(broker, _call("bg_output", {"task": "bg-1"}))

    assert not result.is_error
    assert result.output == "output bg-1 from 0"
    assert registry.calls == [("output", "bg-1", 0)]
    assert jail.exec_commands == []
    assert jail.background_commands == []


def test_control_stop_still_honors_policy():
    registry = FakeRegistry()
    broker = Broker(
        _control_manifest("bg_stop", "stop", policy="ask-always"),
        FakeJail(),
        PolicyEngine(prompt=lambda *_: False),
        registry=registry,
    )

    result = _handle(broker, _call("bg_stop", {"task": "bg-1"}))

    assert result.is_error
    assert "denied" in result.output
    assert registry.calls == []  # declined before the registry is touched


def test_control_unknown_task_is_reported_as_error():
    class RaisingRegistry(FakeRegistry):
        def status(self, task_id):
            raise BackgroundError(f"unknown background task {task_id!r}")

    broker = Broker(
        _control_manifest("bg_status", "status"),
        FakeJail(),
        PolicyEngine(),
        registry=RaisingRegistry(),
    )

    result = _handle(broker, _call("bg_status", {"task": "bg-9"}))

    assert result.is_error
    assert "unknown background task" in result.output


def test_control_output_rejects_non_integer_offset_via_broker_dispatch():
    """The broker's control dispatcher converts a bad offset into a BackgroundError."""
    broker = Broker(
        _control_manifest("bg_output", "output"),
        FakeJail(),
        PolicyEngine(),
        registry=FakeRegistry(),
    )
    with pytest.raises(BackgroundError, match="offset must be an integer"):
        broker._dispatch_control("output", {"task": "bg-1", "offset": "abc"})


def test_validate_args_enforces_types_and_enums():
    params = {
        "direction": Param(
            type="string",
            description="",
            required=True,
            enum=["up", "down"],
        ),
        "steps": Param(type="integer", description=""),
    }

    assert validate_args({"direction": "up"}, params) is None

    error = validate_args({"direction": "sideways"}, params)
    assert error is not None
    assert "must be one of" in error

    error = validate_args({"direction": "up", "steps": True}, params)
    assert error is not None
    assert "must be an integer" in error

    error = validate_args({"direction": "up", "bogus": 1}, params)
    assert error is not None
    assert "unknown parameter" in error


def test_interpolate_shell_escapes_every_value():
    command = interpolate("echo {message}", {"message": "a; rm -rf /"})

    assert command == "echo 'a; rm -rf /'"


def test_shell_injection_is_escaped_before_it_reaches_the_jail():
    jail = FakeJail()
    broker = _broker(jail)

    _handle(broker, _call("echo", {"message": "hi; echo pwned"}))

    # The payload is quoted into a single argument; the injected command can't run.
    assert jail.exec_commands == ["echo 'hi; echo pwned'"]
