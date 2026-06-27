import json

from tartarus.audit import AuditEvent, FileAuditLog
from tartarus.jail import ExecResult
from tartarus.manifest import Capability, Grant
from tartarus.models import ToolResult
from tartarus.policy import Decision


def test_file_audit_log_appends_jsonl_record(tmp_path):
    audit_path = tmp_path / "audit" / "events.jsonl"
    capability = Capability(
        name="echo",
        description="Echo.",
        policy="auto",
        params={},
        grants=Grant(package_bins=["/nix/store/jq/bin"], writable=["artifacts"]),
        runner="echo hello",
    )
    event = AuditEvent(
        call_id="call-1",
        tool_name="echo",
        arguments={"message": "hello"},
        capability=capability,
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

    FileAuditLog(str(audit_path)).record(event)

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
