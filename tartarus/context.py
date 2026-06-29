"""Context ledger, selection, and deterministic compaction.

Sessions remain the provider-native transcript of record. The context layer is a
derived, append-only audit trail plus a selector that decides which messages are
sent to the provider for the next round-trip.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

CONTEXT_SUFFIX = ".jsonl"
DEFAULT_CONTEXT_MAX_CHARS = 120_000
DEFAULT_CONTEXT_RECENT_TURNS = 20
SUMMARY_ROLE = "system"
DEFAULT_LEDGER_READ_LIMIT = 20
MAX_LEDGER_READ_LIMIT = 100
SUMMARY_LINE_MAX_CHARS = 240
ELLIPSIS = "..."
CONTEXT_TOOL_NAMES = {"context_status", "context_read"}
CONTEXT_TOOLS = [
    {
        "name": "context_status",
        "description": "Read the current conversation context status.",
        "parameters": {
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
    },
    {
        "name": "context_read",
        "description": "Read recent append-only context ledger events.",
        "parameters": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LEDGER_READ_LIMIT,
                    "description": "Maximum number of latest ledger events to return.",
                }
            },
            "additionalProperties": False,
        },
    },
]


class ContextError(Exception):
    """Raised when context state cannot be read or written."""


@dataclass(frozen=True)
class ContextLimits:
    max_chars: int = DEFAULT_CONTEXT_MAX_CHARS
    recent_turns: int = DEFAULT_CONTEXT_RECENT_TURNS


@dataclass(frozen=True)
class ContextStatus:
    message_count: int
    estimated_chars: int
    ledger_event_count: int
    effective_message_count: int
    effective_estimated_chars: int
    ledger_path: str | None


class ContextLedger:
    def __init__(self, context_dir: str, session_id: str):
        self._dir = os.path.abspath(context_dir)
        self.session_id = session_id
        self._path = os.path.join(self._dir, session_id + CONTEXT_SUFFIX)

    @property
    def path(self) -> str:
        return self._path

    def append_event(self, event: dict[str, Any]) -> None:
        if self._dir:
            os.makedirs(self._dir, exist_ok=True)
        enriched = {"created_at": _timestamp(), **event}
        try:
            with open(self._path, "a", encoding="utf-8") as context_file:
                context_file.write(json.dumps(enriched) + "\n")
        except OSError as error:
            raise ContextError(
                f"cannot write context ledger {self._path}: {error}"
            ) from error

    def append_message_events(self, messages: list[dict], start_index: int) -> None:
        for offset, message in enumerate(messages[start_index:], start=start_index):
            self.append_event(message_event(offset, message))

    def load_events(self) -> list[dict[str, Any]]:
        try:
            with open(self._path, encoding="utf-8") as context_file:
                return [json.loads(line) for line in context_file if line.strip()]
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError) as error:
            raise ContextError(
                f"cannot read context ledger {self._path}: {error}"
            ) from error


class ContextManager:
    def __init__(
        self,
        ledger: ContextLedger | None = None,
        limits: ContextLimits | None = None,
        auto_compact: bool = False,
    ):
        self._ledger = ledger
        self._limits = limits or ContextLimits()
        self._auto_compact = auto_compact

    @property
    def ledger_path(self) -> str | None:
        return self._ledger.path if self._ledger is not None else None

    def effective_messages(self, messages: list[dict]) -> list[dict]:
        events = self._load_events()
        summary = latest_summary(events)
        if summary is None:
            return list(messages)

        # Keep every message after the summarized range; nothing between the
        # summary and the recent turns is dropped. fit_to_limit trims oldest
        # whole turns only when the result exceeds max_chars.
        covered_end = int(summary["covered"]["end"])
        suffix_start = valid_boundary_start(messages, covered_end)
        suffix = messages[suffix_start:]
        summary_message = {
            "role": SUMMARY_ROLE,
            "content": "Context summary from earlier transcript:\n\n"
            + str(summary["summary"]),
        }
        return fit_to_limit([summary_message], suffix, self._limits)

    def status(self, messages: list[dict]) -> ContextStatus:
        events = self._load_events()
        effective = self.effective_messages(messages)
        return ContextStatus(
            message_count=len(messages),
            estimated_chars=estimate_messages(messages),
            ledger_event_count=len(events),
            effective_message_count=len(effective),
            effective_estimated_chars=estimate_messages(effective),
            ledger_path=self.ledger_path,
        )

    def read_events(
        self, limit: int = DEFAULT_LEDGER_READ_LIMIT
    ) -> list[dict[str, Any]]:
        events = self._load_events()
        bounded_limit = max(1, min(limit, MAX_LEDGER_READ_LIMIT))
        return events[-bounded_limit:]

    def handle_tool(
        self, name: str, arguments: dict[str, Any], messages: list[dict]
    ) -> str:
        if name == "context_status":
            return json.dumps(asdict(self.status(messages)), sort_keys=True)
        if name == "context_read":
            limit = arguments.get("limit", DEFAULT_LEDGER_READ_LIMIT)
            if not isinstance(limit, int):
                limit = DEFAULT_LEDGER_READ_LIMIT
            return json.dumps(self.read_events(limit), sort_keys=True)
        raise ContextError(f"unknown context tool '{name}'")

    def compact(self, messages: list[dict]) -> dict[str, Any] | None:
        if self._ledger is None:
            raise ContextError("cannot compact without a context ledger")
        if not messages:
            return None

        suffix_start = recent_turn_start(messages, self._limits.recent_turns)
        suffix_start = valid_boundary_start(messages, suffix_start)
        if suffix_start <= 0:
            return None

        # Compaction is monotonic: never re-summarize a range the latest summary
        # already covers, so repeated or automatic compaction appends a new
        # summary only when it advances the covered boundary.
        latest = latest_summary(self._load_events())
        if latest is not None and int(latest["covered"]["end"]) >= suffix_start:
            return None

        summary_text = deterministic_summary(messages[:suffix_start])
        event = {
            "type": "context_summary",
            "covered": {"start": 0, "end": suffix_start},
            "summary": summary_text,
            "source": "deterministic-local",
            "estimated_chars": len(summary_text),
        }
        self._ledger.append_event(event)
        return event

    def maybe_compact(self, messages: list[dict]) -> dict[str, Any] | None:
        """Auto-compact at a turn boundary when the agent opts in and is over limit.

        A no-op unless `autoCompact` is enabled; otherwise it compacts only when
        the effective context already exceeds `max_chars`, reusing the
        deterministic compactor so the result stays an explicit ledger event.
        """
        if not self._auto_compact or self._ledger is None:
            return None
        if (
            estimate_messages(self.effective_messages(messages))
            <= self._limits.max_chars
        ):
            return None
        return self.compact(messages)

    def _load_events(self) -> list[dict[str, Any]]:
        if self._ledger is None:
            return []
        return self._ledger.load_events()


def message_event(index: int, message: dict) -> dict[str, Any]:
    role = str(message.get("role", "unknown"))
    event_type = {
        "user": "user_turn",
        "assistant": "assistant_turn",
        "tool": "tool_result",
    }.get(role, "transcript_message")
    if role == "user" and _is_background_notice(message):
        event_type = "background_notice"
    return {
        "type": event_type,
        "message_index": index,
        "role": role,
        "message": message,
        "estimated_chars": estimate_message(message),
    }


def latest_summary(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    summaries = [event for event in events if event.get("type") == "context_summary"]
    return summaries[-1] if summaries else None


def recent_turn_start(messages: list[dict], recent_turns: int) -> int:
    if recent_turns <= 0:
        return len(messages)

    seen = 0
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            seen += 1
            if seen == recent_turns:
                return index
    return 0


def valid_boundary_start(messages: list[dict], start: int) -> int:
    start = max(0, min(start, len(messages)))
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1
    return start


def fit_to_limit(
    prefix: list[dict],
    suffix: list[dict],
    limits: ContextLimits,
) -> list[dict]:
    candidate = list(prefix) + list(suffix)
    if estimate_messages(candidate) <= limits.max_chars:
        return candidate

    # Over the configured ceiling: drop oldest whole turns from the suffix front
    # (each cut starts at a user message), never mid-turn. This is the explicit
    # max_chars boundary, not silent loss.
    for index, message in enumerate(suffix):
        if message.get("role") != "user":
            continue
        candidate = list(prefix) + suffix[index:]
        if estimate_messages(candidate) <= limits.max_chars:
            return candidate
    return candidate


def estimate_message(message: dict) -> int:
    return len(json.dumps(message, sort_keys=True))


def estimate_messages(messages: list[dict]) -> int:
    return sum(estimate_message(message) for message in messages)


def deterministic_summary(messages: list[dict]) -> str:
    lines = [
        "# Context Summary",
        "",
        f"Covered transcript messages: 0-{len(messages)}",
        "",
    ]
    for index, message in enumerate(messages):
        role = message.get("role", "unknown")
        content = _message_content(message)
        if role == "assistant" and message.get("tool_calls"):
            lines.append(f"- assistant message {index}: requested tools")
            for tool_call in message["tool_calls"]:
                function = tool_call.get("function", {})
                name = function.get("name") or tool_call.get("name") or "unknown"
                lines.append(f"  - tool call: {name}")
            continue
        if role == "tool":
            tool_call_id = message.get("tool_call_id", "unknown")
            lines.append(
                f"- tool result {index} ({tool_call_id}): {_one_line(content)}"
            )
            continue
        lines.append(f"- {role} message {index}: {_one_line(content)}")
    return "\n".join(lines)


def _message_content(message: dict) -> str:
    content = message.get("content", "")
    if isinstance(content, str):
        return content
    return json.dumps(content, sort_keys=True)


def _one_line(text: str) -> str:
    compact = " ".join(text.split())
    if len(compact) > SUMMARY_LINE_MAX_CHARS:
        return compact[: SUMMARY_LINE_MAX_CHARS - len(ELLIPSIS)] + ELLIPSIS
    return compact


def _is_background_notice(message: dict) -> bool:
    content = message.get("content")
    return isinstance(content, str) and content.startswith("[background] ")


def _timestamp() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
