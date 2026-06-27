"""The Broker: resolve → validate → policy → build jail → exec → result (§6.5).

Each tool call is validated, gated by the PolicyEngine (which may prompt the
human), then run inside a bwrap jail via the injected JailBuilder. Argument
validation and shell-safe interpolation are permanent and shared by all phases.
"""

import shlex
from collections import defaultdict
from collections.abc import Callable
from threading import Event

from tartarus.audit import AuditEvent, AuditSink, NullAuditLog
from tartarus.background import BackgroundError, BackgroundRegistry
from tartarus.jail import JailBuilder, JailError
from tartarus.manifest import Capability, Manifest, Param
from tartarus.models import ToolCall, ToolResult
from tartarus.policy import Decision, PolicyEngine

DEFAULT_OUTPUT_TRUNCATE_CHARS = 10_000

# Declared param type -> the Python type a valid argument must be an instance of.
_JSON_TYPE_TO_PYTHON = {
    "string": str,
    "integer": int,
    "boolean": bool,
    "array": list,
}


def validate_args(arguments: dict, params: dict[str, Param]) -> str | None:
    """Return an error string if arguments violate the schema, else None."""
    for name, param in params.items():
        if param.required and name not in arguments:
            return f"missing required parameter '{name}'"

    for name, value in arguments.items():
        param: Param | None = params.get(name)
        if param is None:
            return f"unknown parameter '{name}'"
        type_error = _check_type(name, value, param)
        if type_error:
            return type_error
        if param.enum is not None and value not in param.enum:
            return f"parameter '{name}' must be one of {param.enum}"
    return None


def _check_type(name: str, value, param: Param) -> str | None:
    expected = _JSON_TYPE_TO_PYTHON.get(param.type)
    if expected is None:
        return None  # unknown declared type: do not block

    # bool is a subclass of int in Python; reject it where an integer is expected.
    if param.type == "integer" and isinstance(value, bool):
        return f"parameter '{name}' must be an integer"
    if not isinstance(value, expected):
        return f"parameter '{name}' must be a {param.type}"
    return None


def interpolate(runner: str, arguments: dict) -> str:
    """Fill a runner template's {placeholders}, shell-escaping every value.

    Every model-supplied value is passed through shlex.quote so untrusted text can
    never break out of its argument position (PLAN.md §8.5).
    """
    safe_values = defaultdict(
        lambda: shlex.quote(""),
        {key: shlex.quote(str(value)) for key, value in arguments.items()},
    )
    return runner.format_map(safe_values)


class Broker:
    """Resolves tool calls to capabilities and runs them inside the jail."""

    def __init__(
        self,
        manifest: Manifest,
        jail: JailBuilder,
        policy: PolicyEngine,
        output_truncate: int = DEFAULT_OUTPUT_TRUNCATE_CHARS,
        audit: AuditSink | None = None,
        registry: BackgroundRegistry | None = None,
    ):
        self._manifest = manifest
        self._jail = jail
        self._policy = policy
        self._output_truncate = output_truncate
        self._audit = audit if audit is not None else NullAuditLog()
        self._registry = registry

    def handle(
        self,
        call: ToolCall,
        output_callback: Callable[[str], None] | None = None,
        cancellation: Event | None = None,
    ) -> ToolResult:
        if call.argument_error:
            return self._audited_error(
                call,
                f"invalid arguments: {call.argument_error}",
                decision=_broker_decision(f"invalid arguments: {call.argument_error}"),
            )

        capability = self._manifest.capabilities.get(call.name)
        if capability is None:
            return self._audited_error(
                call,
                f"unknown tool '{call.name}'",
                decision=_broker_decision("unknown tool"),
            )

        validation_error = validate_args(call.arguments, capability.params)
        if validation_error:
            return self._audited_error(
                call,
                f"invalid arguments: {validation_error}",
                capability=capability,
                decision=_broker_decision(f"invalid arguments: {validation_error}"),
            )

        return self._run(
            call.id,
            capability,
            call.arguments,
            output_callback=output_callback,
            cancellation=cancellation,
        )

    def _run(
        self,
        call_id: str,
        capability: Capability,
        arguments: dict,
        output_callback: Callable[[str], None] | None = None,
        cancellation: Event | None = None,
    ) -> ToolResult:
        command = interpolate(capability.runner, arguments)

        decision = self._policy.decide(capability, arguments, command)
        if not decision.allowed:
            return self._finish(
                AuditEvent(
                    call_id=call_id,
                    tool_name=capability.name,
                    arguments=arguments,
                    capability=capability,
                    command=command,
                    decision=decision,
                    result=_error(call_id, f"denied: {decision.reason}"),
                    broker_error=f"denied: {decision.reason}",
                )
            )

        if capability.kind == "control":
            return self._run_control(call_id, capability, arguments, command, decision)
        if capability.kind == "background":
            return self._run_background(
                call_id, capability, arguments, command, decision
            )
        return self._run_command(
            call_id,
            capability,
            arguments,
            command,
            decision,
            output_callback=output_callback,
            cancellation=cancellation,
        )

    def _run_command(
        self,
        call_id: str,
        capability: Capability,
        arguments: dict,
        command: str,
        decision: Decision,
        output_callback: Callable[[str], None] | None = None,
        cancellation: Event | None = None,
    ) -> ToolResult:
        # A capability runs unbounded unless it declares its own timeout;
        # None means "wait forever" in the jail's process wait loop.
        try:
            spec = self._jail.build(capability.grants)
            result = self._jail.exec(
                spec,
                command,
                timeout=capability.timeout,
                output_callback=output_callback,
                cancellation=cancellation,
            )
        except JailError as error:
            return self._jail_error(
                call_id, capability, arguments, command, decision, error
            )

        output = _format_output(result.stdout, result.stderr, self._output_truncate)
        return self._finish(
            AuditEvent(
                call_id=call_id,
                tool_name=capability.name,
                arguments=arguments,
                capability=capability,
                command=command,
                decision=decision,
                exec_result=result,
                result=ToolResult(call_id, output, is_error=result.code != 0),
            )
        )

    def _run_background(
        self,
        call_id: str,
        capability: Capability,
        arguments: dict,
        command: str,
        decision: Decision,
    ) -> ToolResult:
        if self._registry is None:
            return self._audited_error(
                _call_stub(call_id, capability.name, arguments),
                "background execution is not available",
                decision=decision,
                capability=capability,
            )
        try:
            spec = self._jail.build(capability.grants)
            handle = self._jail.exec_background(spec, command)
        except JailError as error:
            return self._jail_error(
                call_id, capability, arguments, command, decision, error
            )

        bg_id = self._registry.register(capability.name, handle)
        # Launch is non-terminal: its result is just the handle. The task's real
        # output and exit arrive later via the control tools / a completion notice.
        return self._finish(
            AuditEvent(
                call_id=call_id,
                tool_name=capability.name,
                arguments=arguments,
                capability=capability,
                command=command,
                decision=decision,
                result=ToolResult(call_id, f"started {bg_id}", is_error=False),
            )
        )

    def _run_control(
        self,
        call_id: str,
        capability: Capability,
        arguments: dict,
        command: str,
        decision: Decision,
    ) -> ToolResult:
        if self._registry is None:
            return self._audited_error(
                _call_stub(call_id, capability.name, arguments),
                "background control is not available",
                decision=decision,
                capability=capability,
            )
        try:
            output = self._dispatch_control(capability.control, arguments)
            result = ToolResult(call_id, output, is_error=False)
        except BackgroundError as error:
            result = _error(call_id, str(error))
        return self._finish(
            AuditEvent(
                call_id=call_id,
                tool_name=capability.name,
                arguments=arguments,
                capability=capability,
                command=command,
                decision=decision,
                result=result,
            )
        )

    def _dispatch_control(self, control: str | None, arguments: dict) -> str:
        assert self._registry is not None
        task = arguments.get("task")
        if control == "status":
            return self._registry.status(task)
        if control == "output":
            try:
                offset = int(arguments.get("offset", 0))
            except (ValueError, TypeError) as exc:
                raise BackgroundError(f"offset must be an integer: {exc}") from exc
            return self._registry.output(task, offset)
        if control == "stop":
            return self._registry.stop(task)
        raise BackgroundError(f"unknown control op {control!r}")

    def _jail_error(
        self,
        call_id: str,
        capability: Capability,
        arguments: dict,
        command: str,
        decision: Decision,
        error: JailError,
    ) -> ToolResult:
        return self._finish(
            AuditEvent(
                call_id=call_id,
                tool_name=capability.name,
                arguments=arguments,
                capability=capability,
                command=command,
                decision=decision,
                result=_error(call_id, f"jail error: {error}"),
                broker_error=f"jail error: {error}",
            )
        )

    def _audited_error(
        self,
        call: ToolCall,
        message: str,
        decision: Decision,
        capability: Capability | None = None,
    ) -> ToolResult:
        return self._finish(
            AuditEvent(
                call_id=call.id,
                tool_name=call.name,
                arguments=call.arguments,
                capability=capability,
                decision=decision,
                result=_error(call.id, message),
                broker_error=message,
            )
        )

    def _finish(self, event: AuditEvent) -> ToolResult:
        self._audit.record(event)
        return event.result


def _call_stub(call_id: str, name: str, arguments: dict) -> ToolCall:
    """Reconstruct a ToolCall for the error-auditing path (already validated)."""
    return ToolCall(id=call_id, name=name, arguments=arguments)


def _error(call_id: str, message: str) -> ToolResult:
    return ToolResult(call_id, f"error: {message}", is_error=True)


def _broker_decision(reason: str) -> Decision:
    return Decision(False, reason, "broker")


def _format_output(stdout: str, stderr: str, output_truncate: int) -> str:
    combined = "\n".join(part for part in (stdout, stderr) if part).strip()
    if not combined:
        return "(no output)"
    if len(combined) > output_truncate:
        return combined[:output_truncate] + "\n...(truncated)"
    return combined
