"""The Provider protocol.

The AgentLoop talks to providers in provider-neutral terms; each concrete provider
adapts to exactly one wire format. Completion is async so the harness can drive the
backend without blocking the event loop.
"""

from collections.abc import AsyncIterator
from typing import Protocol

from tartarus.models import AssistantTurn, StreamEvent, ToolResult


class Provider(Protocol):
    async def complete(
        self,
        system: str,
        messages: list[dict],  # provider-native running transcript
        tools: list[dict],  # provider-neutral manifest tools
    ) -> AssistantTurn: ...

    def stream(
        self,
        system: str,
        messages: list[dict],  # provider-native running transcript
        tools: list[dict],  # provider-neutral manifest tools
    ) -> AsyncIterator[StreamEvent]:
        """Stream a turn: yield TextDelta as text arrives, ending in TurnComplete.

        This is the path the AgentLoop drives; `complete` remains for non-streaming
        callers. Implementations return an async iterator, usually an async
        generator.
        """
        ...

    def assistant_message(self, turn: AssistantTurn) -> dict:
        """Build the provider-native assistant message to append after a turn."""
        ...

    def tool_result_messages(self, results: list[ToolResult]) -> list[dict]:
        """Build provider-native tool-result message(s) for a batch of results."""
        ...

    def adapt_tools(self, tools: list[dict]) -> list[dict]:
        """Adapt provider-neutral manifest tools to this provider's tool schema."""
        ...
