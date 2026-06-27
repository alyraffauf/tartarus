"""Append-only JSONL audit log for brokered tool calls."""

import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from tartarus.jail import ExecResult
from tartarus.manifest import Capability, Grant
from tartarus.models import ToolResult
from tartarus.policy import Decision


class AuditError(Exception):
    """Raised when an audit record cannot be written."""


class AuditSink(Protocol):
    def record(self, event: "AuditEvent") -> None: ...


@dataclass(frozen=True)
class AuditEvent:
    call_id: str
    tool_name: str
    arguments: dict
    result: ToolResult
    capability: Capability | None = None
    command: str | None = None
    decision: Decision | None = None
    exec_result: ExecResult | None = None
    broker_error: str | None = None


class NullAuditLog:
    """Test/default sink for callers that do not configure file auditing."""

    def record(self, event: AuditEvent) -> None:
        return None


class FileAuditLog:
    def __init__(self, path: str):
        self._path = os.path.abspath(path)

    def record(self, event: AuditEvent) -> None:
        record = _event_to_json(event)
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        try:
            with open(self._path, "a", encoding="utf-8") as audit_file:
                audit_file.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as error:
            raise AuditError(f"cannot write audit log {self._path}: {error}") from error


def _event_to_json(event: AuditEvent) -> dict:
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "call_id": event.call_id,
        "tool_name": event.tool_name,
        "capability_name": _capability_name(event.capability),
        "arguments": event.arguments,
        "policy": _decision_json(event.decision),
        "command": event.command,
        "grants": _grant_json(event.capability.grants if event.capability else None),
        "exit_code": event.exec_result.code if event.exec_result else None,
        "network_summary": _network_summary(event),
        "output_length": len(event.result.output),
        "is_error": event.result.is_error,
        "broker_error": event.broker_error,
    }


def _capability_name(capability: Capability | None) -> str | None:
    if capability is None:
        return None
    return capability.name


def _decision_json(decision: Decision | None) -> dict | None:
    if decision is None:
        return None
    return {
        "allowed": decision.allowed,
        "reason": decision.reason,
        "approver": decision.approver,
    }


def _grant_json(grant: Grant | None) -> dict | None:
    if grant is None:
        return None
    return {
        "package_bins": grant.package_bins,
        "allowed_hosts": grant.allowed_hosts,
        "writable": grant.writable,
        "unrestricted": grant.unrestricted,
    }


def _network_summary(event: AuditEvent) -> str | None:
    if event.exec_result is None:
        return None
    return event.exec_result.network_summary
