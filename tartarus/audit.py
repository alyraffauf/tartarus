"""Append-only JSONL audit log for brokered tool calls."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from tartarus.jail import ExecResult
from tartarus.manifest import Capability, Grant
from tartarus.models import ToolResult
from tartarus.policy import Decision


class AuditError(Exception):
    """Raised when an audit record cannot be written."""


class AuditEvent(BaseModel):
    """A single brokered tool call, recorded to the audit log as JSONL."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    call_id: str
    tool_name: str
    arguments: dict[str, Any]
    result: ToolResult
    # Pinned at event construction, so serializing the same event twice (e.g. a
    # retried write) records the same time rather than re-reading the clock.
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    capability: Capability | None = None
    command: str | None = None
    decision: Decision | None = None
    exec_result: ExecResult | None = None
    broker_error: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "call_id": self.call_id,
            "tool_name": self.tool_name,
            "capability_name": self.capability.name if self.capability else None,
            "arguments": self.arguments,
            "policy": self._decision_dict(self.decision),
            "command": self.command,
            "grants": self._grant_dict(
                self.capability.grants if self.capability else None
            ),
            "exit_code": self.exec_result.code if self.exec_result else None,
            "network_summary": (
                self.exec_result.network_summary if self.exec_result else None
            ),
            "output_length": len(self.result.output),
            "is_error": self.result.is_error,
            "broker_error": self.broker_error,
        }

    @staticmethod
    def _decision_dict(decision: Decision | None) -> dict[str, Any] | None:
        if decision is None:
            return None
        return {
            "allowed": decision.allowed,
            "reason": decision.reason,
            "approver": decision.approver,
        }

    @staticmethod
    def _grant_dict(grant: Grant | None) -> dict[str, Any] | None:
        if grant is None:
            return None
        return {
            "package_bins": grant.package_bins,
            "allowed_hosts": grant.allowed_hosts,
            "writable": grant.writable,
            "unrestricted": grant.unrestricted,
        }


class AuditSink(Protocol):
    def record(self, event: AuditEvent) -> None: ...


class NullAuditLog:
    """Test/default sink for callers that do not configure file auditing."""

    def record(self, event: AuditEvent) -> None:
        return None


class FileAuditLog(AuditSink):
    def __init__(self, path: str):
        self._path = os.path.abspath(path)

    def record(self, event: AuditEvent) -> None:
        record = event.to_json_dict()
        directory = os.path.dirname(self._path)
        if directory:
            os.makedirs(directory, exist_ok=True)

        try:
            with open(self._path, "a", encoding="utf-8") as audit_file:
                audit_file.write(json.dumps(record, sort_keys=True) + "\n")
        except OSError as error:
            raise AuditError(f"cannot write audit log {self._path}: {error}") from error
