"""Provider-neutral data types shared by the AgentLoop, Provider, and Broker.

These shapes are deliberately independent of any vendor wire format. Each concrete
Provider translates between these and its backend's request/response JSON, so the
AgentLoop and Broker never branch on which backend is configured.
"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ToolCall:
    """A model's request to invoke one tool, normalized across providers."""

    id: str
    name: str
    arguments: dict
    # Set when the provider could not parse the model's raw arguments as a JSON
    # object. The broker turns this into an error result so the model can retry,
    # rather than the harness crashing on malformed output.
    argument_error: str | None = None


@dataclass
class AssistantTurn:
    """One assistant response, normalized across providers."""

    text: str | None
    tool_calls: list[ToolCall]
    raw: dict[str, Any]  # provider-native message, appended to the transcript verbatim
    stop_reason: str  # "tool_calls" | "end" | "length" | "error"


@dataclass
class ToolResult:
    """The outcome of brokering one tool call.

    The provider renders this into its backend's native tool-result message.
    """

    call_id: str
    output: str
    is_error: bool


# --- Streaming events ---------------------------------------------------------
#
# A provider's stream() yields these as a turn is generated. The AgentLoop
# forwards TextDelta to the UI as text arrives and uses the terminal
# TurnComplete to broker any tool calls and extend the transcript. Keeping
# these provider-neutral means the loop never branches on the backend.


@dataclass
class TextDelta:
    """A chunk of assistant text produced mid-turn."""

    text: str


@dataclass
class ToolOutputDelta:
    """A chunk of command output produced while a tool is still running."""

    call: ToolCall
    text: str


@dataclass
class TurnComplete:
    """Terminal stream event carrying the fully assembled turn."""

    turn: AssistantTurn


# What a provider's stream() yields.
StreamEvent = TextDelta | TurnComplete
