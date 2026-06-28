import json

import pytest

from tartarus.audit import AuditEvent, FileAuditLog, NullAuditLog
from tartarus.broker import _format_output
from tartarus.jail import ExecResult
from tartarus.manifest import Capability, Grant
from tartarus.models import ToolResult
from tartarus.policy import Decision


def _echo_capability():
    return Capability(
        name="echo",
        description="Echo.",
        policy="auto",
        params={},
        grants=Grant(package_bins=["/nix/store/jq/bin"], writable=["artifacts"]),
        runner="echo hello",
    )


def _full_event():
    return AuditEvent(
        call_id="call-1",
        tool_name="echo",
        arguments={"message": "hello"},
        capability=_echo_capability(),
        command="echo hello",
        decision=Decision(True, "auto policy", "auto"),
        exec_result=ExecResult(
            0,
            "hello\n",
            "",
            network_summary="proxy decisions: 1 allowed, 0 blocked",
        ),
        result=ToolResult("call-1", "hello", is_error=False),
    )


def test_file_audit_log_appends_jsonl_record(tmp_path):
    audit_path = tmp_path / "audit" / "events.jsonl"

    FileAuditLog(str(audit_path)).record(_full_event())

    lines = audit_path.read_text().splitlines()
    assert len(lines) == 1

    record = json.loads(lines[0])
    assert record["call_id"] == "call-1"
    assert record["tool_name"] == "echo"
    assert record["capability_name"] == "echo"
    assert record["arguments"] == {"message": "hello"}
    assert record["policy"] == {
        "allowed": True,
        "reason": "auto policy",
        "approver": "auto",
    }
    assert record["command"] == "echo hello"
    assert record["grants"] == {
        "package_bins": ["/nix/store/jq/bin"],
        "allowed_hosts": [],
        "writable": ["artifacts"],
        "unrestricted": False,
    }
    assert record["exit_code"] == 0
    assert record["network_summary"] == "proxy decisions: 1 allowed, 0 blocked"
    assert record["output_length"] == 5
    assert record["is_error"] is False


def test_file_audit_log_appends_multiple_events(tmp_path):
    audit_path = tmp_path / "audit" / "events.jsonl"

    FileAuditLog(str(audit_path)).record(_full_event())
    FileAuditLog(str(audit_path)).record(_full_event())

    assert len(audit_path.read_text().splitlines()) == 2


def test_audit_event_serializes_none_fields(tmp_path):
    """Events from broker-rejection paths (unknown tool, jail error) carry None
    for the optional fields that didn't populate."""
    event = AuditEvent(
        call_id="call-2",
        tool_name="unknown",
        arguments={},
        result=ToolResult("call-2", "error: unknown tool", is_error=True),
        command=None,
        decision=None,
        exec_result=None,
        broker_error="unknown tool",
    )
    audit_path = tmp_path / "audit" / "events.jsonl"
    FileAuditLog(str(audit_path)).record(event)

    record = json.loads(audit_path.read_text().splitlines()[0])
    assert record["call_id"] == "call-2"
    assert record["capability_name"] is None
    assert record["command"] is None
    assert record["policy"] is None
    assert record["exit_code"] is None
    assert record["network_summary"] is None
    assert record["broker_error"] == "unknown tool"


def test_null_audit_log_is_noop():
    NullAuditLog().record(_full_event())  # must not raise


@pytest.mark.parametrize(
    "stdout,stderr,output_truncate,expected",
    [
        ("a", "b", 100, "a\nb"),
        ("", "", 100, "(no output)"),
        ("", "err", 100, "err"),
        ("abcdef", "", 3, "abc\n...(truncated)"),
    ],
)
def test_format_output(stdout, stderr, output_truncate, expected):
    assert _format_output(stdout, stderr, output_truncate) == expected
