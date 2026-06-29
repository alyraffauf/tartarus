"""The provider-agnostic agent loop (PLAN.md §6.4).

The loop never branches on which provider is configured: it streams a turn from
the provider, forwards text to the caller as it arrives, brokers any tool calls,
and feeds the results back to extend the transcript.

`run_turn` is an async generator of UI events (`TextDelta`, `ToolStarted`,
`ToolOutputDelta`, `ToolFinished`). The transcript (`messages`) is mutated only
at whole-round-trip boundaries — the assistant message and *all* its tool results
are appended together once a round-trip finishes — so cancelling the iteration
mid-stream or mid-tool always leaves a valid transcript.
"""

import asyncio
from dataclasses import dataclass

from tartarus.broker import Broker
from tartarus.context import CONTEXT_TOOL_NAMES, CONTEXT_TOOLS, ContextManager
from tartarus.manifest import Manifest
from tartarus.models import (
    TextDelta,
    ToolCall,
    ToolOutputDelta,
    ToolResult,
    TurnComplete,
)
from tartarus.provider.base import Provider


@dataclass
class ToolStarted:
    """Emitted just before a tool call is brokered."""

    call: ToolCall


@dataclass
class ToolFinished:
    """Emitted after a tool call has been brokered."""

    call: ToolCall
    result: ToolResult


class AgentLoop:
    def __init__(
        self,
        provider: Provider,
        broker: Broker,
        manifest: Manifest,
        system_prompt: str,
        context_manager: ContextManager | None = None,
    ):
        self._provider = provider
        self._broker = broker
        self._manifest = manifest
        self._system_prompt = system_prompt
        self._context_manager = context_manager or ContextManager()

    async def run_turn(self, messages: list[dict]):
        """Drive one human turn to completion, yielding UI events as they happen.

        Streams provider round-trips and brokers tool calls until the model stops
        asking for tools. `messages` is extended in place, but only at safe
        boundaries (see module docstring), so an aborted iteration never leaves a
        half-written transcript.
        """
        while True:
            text_parts: list[str] = []
            turn = None
            effective_messages = self._context_manager.effective_messages(messages)
            async for event in self._provider.stream(
                self._system_prompt, effective_messages, self._tools()
            ):
                if isinstance(event, TextDelta):
                    text_parts.append(event.text)
                    yield event
                elif isinstance(event, TurnComplete):
                    turn = event.turn

            assert turn is not None, "stream() must end with TurnComplete"
            assistant_message = self._provider.assistant_message(turn)

            if turn.stop_reason != "tool_calls":
                # No tools: commit the assistant message and finish the turn.
                messages.append(assistant_message)
                return

            results = []
            for call in turn.tool_calls:
                yield ToolStarted(call)
                result = None
                async for tool_event in self._run_tool(call, messages):
                    if isinstance(tool_event, ToolOutputDelta):
                        yield tool_event
                    else:
                        result = tool_event
                        yield ToolFinished(call, result)
                assert result is not None
                results.append(result)

            # Commit the whole round-trip atomically: assistant message + every
            # tool result together, so the transcript is always a valid sequence.
            messages.append(assistant_message)
            messages.extend(self._provider.tool_result_messages(results))

    async def _run_tool(self, call: ToolCall, messages: list[dict]):
        # Context tools only inspect local context state — no host reach to gate —
        # so they answer here directly, bypassing the broker/jail/policy/audit path.
        if call.name in CONTEXT_TOOL_NAMES:
            yield ToolResult(
                call.id,
                self._context_manager.handle_tool(call.name, call.arguments, messages),
                is_error=False,
            )
            return

        output_queue: asyncio.Queue[str] = asyncio.Queue()

        # The broker runs on this loop, so streamed output lands on the queue
        # directly.
        def emit_output(text: str) -> None:
            output_queue.put_nowait(text)

        worker = asyncio.create_task(self._broker.handle(call, emit_output))
        pending_get: asyncio.Task[str] | None = None
        try:
            while True:
                if pending_get is None:
                    pending_get = asyncio.create_task(output_queue.get())
                done, _pending = await asyncio.wait(
                    {worker, pending_get}, return_when=asyncio.FIRST_COMPLETED
                )
                if pending_get in done:
                    text = pending_get.result()
                    pending_get = None
                    yield ToolOutputDelta(call, text)
                    continue
                if worker in done:
                    if pending_get is not None:
                        pending_get.cancel()
                        pending_get = None
                    while not output_queue.empty():
                        yield ToolOutputDelta(call, output_queue.get_nowait())
                    yield worker.result()
                    return
        except (asyncio.CancelledError, GeneratorExit):
            # Cancel the worker and await it shielded so the jail tears its
            # process group down before the turn unwinds.
            worker.cancel()
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                pass
            raise
        finally:
            if pending_get is not None:
                pending_get.cancel()

    def _tools(self) -> list[dict]:
        return [*self._manifest.tools, *CONTEXT_TOOLS]
