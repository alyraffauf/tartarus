import pytest

from tartarus.context import (
    ContextError,
    ContextLedger,
    ContextLimits,
    ContextManager,
    deterministic_summary,
    estimate_messages,
    message_event,
    valid_boundary_start,
)
from tartarus.session import SessionStore


def test_ledger_append_and_load_round_trips_events(tmp_path):
    ledger = ContextLedger(str(tmp_path), "s1")
    ledger.append_event({"type": "user_turn", "message_index": 0})
    ledger.append_message_events(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ],
        0,
    )

    events = ContextLedger(str(tmp_path), "s1").load_events()

    assert [event["type"] for event in events] == [
        "user_turn",
        "user_turn",
        "assistant_turn",
    ]
    assert events[1]["message"]["content"] == "hi"


def test_ledger_rejects_corrupt_json(tmp_path):
    path = tmp_path / "s1.jsonl"
    path.write_text("not json\n")

    with pytest.raises(ContextError, match="cannot read context ledger"):
        ContextLedger(str(tmp_path), "s1").load_events()


def test_message_event_classifies_tool_and_background_messages():
    assert message_event(0, {"role": "tool", "content": "ok"})["type"] == "tool_result"
    event = message_event(1, {"role": "user", "content": "[background] t1 done"})
    assert event["type"] == "background_notice"


def test_effective_messages_are_identity_without_summary(tmp_path):
    manager = ContextManager(ContextLedger(str(tmp_path), "s1"))
    messages = [{"role": "user", "content": "hi"}]

    assert manager.effective_messages(messages) == messages
    assert manager.effective_messages(messages) is not messages


def test_effective_messages_keep_all_messages_after_the_summary(tmp_path):
    # The transcript has grown well past the summarized range; everything after
    # covered_end must survive even though it is older than the recent window.
    ledger = ContextLedger(str(tmp_path), "s1")
    ledger.append_event(
        {
            "type": "context_summary",
            "covered": {"start": 0, "end": 2},
            "summary": "Earlier work was completed.",
            "source": "deterministic-local",
            "estimated_chars": 27,
        }
    )
    messages = [
        {"role": "user", "content": "covered 0"},
        {"role": "assistant", "content": "covered 1"},
        {"role": "user", "content": "gap 2"},
        {"role": "assistant", "content": "gap 3"},
        {"role": "user", "content": "recent 4"},
        {"role": "assistant", "content": "recent 5"},
    ]

    effective = ContextManager(
        ledger,
        ContextLimits(max_chars=10_000, recent_turns=1),
    ).effective_messages(messages)

    assert effective[0]["role"] == "system"
    assert effective[1:] == messages[2:]
    assert messages == [
        {"role": "user", "content": "covered 0"},
        {"role": "assistant", "content": "covered 1"},
        {"role": "user", "content": "gap 2"},
        {"role": "assistant", "content": "gap 3"},
        {"role": "user", "content": "recent 4"},
        {"role": "assistant", "content": "recent 5"},
    ]


def test_effective_messages_use_summary_plus_valid_recent_suffix(tmp_path):
    ledger = ContextLedger(str(tmp_path), "s1")
    ledger.append_event(
        {
            "type": "context_summary",
            "covered": {"start": 0, "end": 2},
            "summary": "Earlier: user asked for setup.",
            "source": "deterministic-local",
            "estimated_chars": 31,
        }
    )
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old reply"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new reply"},
    ]

    effective = ContextManager(
        ledger,
        ContextLimits(max_chars=10_000, recent_turns=1),
    ).effective_messages(messages)

    assert effective[0]["role"] == "system"
    assert "Earlier: user asked" in effective[0]["content"]
    assert effective[1:] == messages[2:]


def test_valid_boundary_selection_skips_orphan_tool_results():
    messages = [
        {"role": "user", "content": "run"},
        {"role": "assistant", "tool_calls": [{"id": "call-1"}]},
        {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        {"role": "assistant", "content": "done"},
    ]

    assert valid_boundary_start(messages, 2) == 3


def test_deterministic_compaction_appends_summary_and_preserves_session(tmp_path):
    session = SessionStore(str(tmp_path / "sessions"), "s1")
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old reply"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new reply"},
    ]
    session.append(messages)
    ledger = ContextLedger(str(tmp_path / "context"), "s1")

    event = ContextManager(
        ledger,
        ContextLimits(max_chars=10_000, recent_turns=1),
    ).compact(messages)

    assert event is not None
    assert event["covered"] == {"start": 0, "end": 2}
    assert "user message 0: old" in event["summary"]
    assert SessionStore(str(tmp_path / "sessions"), "s1").load() == messages
    persisted_event = ContextLedger(str(tmp_path / "context"), "s1").load_events()[-1]
    assert persisted_event["type"] == "context_summary"
    assert persisted_event["covered"] == {"start": 0, "end": 2}
    assert persisted_event["summary"] == event["summary"]


def test_status_counts_raw_and_effective_messages(tmp_path):
    ledger = ContextLedger(str(tmp_path), "s1")
    messages = [{"role": "user", "content": "hi"}]

    status = ContextManager(ledger).status(messages)

    assert status.message_count == 1
    assert status.estimated_chars == estimate_messages(messages)
    assert status.ledger_event_count == 0


def test_deterministic_summary_mentions_tool_calls_and_results():
    summary = deterministic_summary(
        [
            {"role": "user", "content": "run it"},
            {
                "role": "assistant",
                "tool_calls": [
                    {"id": "call-1", "function": {"name": "bash"}},
                ],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "ok"},
        ]
    )

    assert "tool call: bash" in summary
    assert "tool result 2 (call-1): ok" in summary
