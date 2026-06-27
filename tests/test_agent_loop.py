import asyncio
import shlex
import subprocess
import threading
from collections.abc import AsyncIterator
from typing import cast

from tartarus.agent_loop import AgentLoop, ToolFinished, ToolStarted
from tartarus.broker import Broker
from tartarus.jail import ExecResult, JailBuilder
from tartarus.models import (
    AssistantTurn,
    StreamEvent,
    TextDelta,
    ToolOutputDelta,
    ToolCall,
    ToolResult,
    TurnComplete,
)
from tartarus.policy import PolicyEngine
from tests.manifest_fixtures import echo_manifest


class LocalJail:
    """A jail double that runs the command directly (no bwrap), so the loop's
    broker integration can be exercised without Linux namespaces."""

    def build(self, grant):
        return grant

    def exec(
        self, spec, command, timeout=None, output_callback=None, cancellation=None
    ):
        completed = subprocess.run(shlex.split(command), capture_output=True, text=True)
        if output_callback is not None and completed.stdout:
            output_callback(completed.stdout)
        return ExecResult(completed.returncode, completed.stdout, completed.stderr)


class ScriptedProvider:
    """A provider that replays a fixed list of turns as streams, recording the
    tool results it is handed back. Stands in for any real backend to prove the
    loop is provider-neutral and brokers tool calls correctly.

    Each turn's text is emitted as a single TextDelta followed by TurnComplete,
    so the streaming loop drives identically to a real SSE backend.
    """

    def __init__(self, turns: list[AssistantTurn]):
        self._turns = list(turns)
        self.received_results: list = []

    async def complete(
        self, system: str, messages: list[dict], tools: list[dict]
    ) -> AssistantTurn:
        return self._turns.pop(0)

    async def stream(
        self, system: str, messages: list[dict], tools: list[dict]
    ) -> AsyncIterator[StreamEvent]:
        turn = self._turns.pop(0)
        if turn.text:
            yield TextDelta(turn.text)
        yield TurnComplete(turn)

    def assistant_message(self, turn: AssistantTurn) -> dict:
        return {"role": "assistant", "content": turn.text or ""}

    def tool_result_messages(self, results: list) -> list[dict]:
        self.received_results.extend(results)
        return [
            {"role": "tool", "tool_call_id": r.call_id, "content": r.output}
            for r in results
        ]

    def adapt_tools(self, tools: list[dict]) -> list[dict]:
        return tools


async def _drain(loop, messages):
    """Collect every UI event run_turn yields for one human turn."""
    events = []
    async for event in loop.run_turn(messages):
        events.append(event)
    return events


def _text(events):
    return "".join(e.text for e in events if isinstance(e, TextDelta))


def test_loop_brokers_tool_calls_then_streams_final_text():
    manifest = echo_manifest()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                text=None,
                tool_calls=[ToolCall("call-1", "echo", {"message": "ping"})],
                raw={"role": "assistant"},
                stop_reason="tool_calls",
            ),
            AssistantTurn(
                text="The tool said ping.",
                tool_calls=[],
                raw={"role": "assistant"},
                stop_reason="end",
            ),
        ]
    )
    loop = AgentLoop(
        provider,
        Broker(manifest, cast(JailBuilder, LocalJail()), PolicyEngine()),
        manifest,
        "system",
    )

    messages = [{"role": "user", "content": "use echo"}]
    events = asyncio.run(_drain(loop, messages))

    assert _text(events) == "The tool said ping."
    # A ToolStarted/ToolFinished pair surfaced the brokered call.
    assert any(isinstance(e, ToolStarted) for e in events)
    finished = [e for e in events if isinstance(e, ToolFinished)]
    assert finished and finished[0].result.output == "ping"
    assert len(provider.received_results) == 1
    # transcript: user, assistant(tool_calls), tool result, assistant(final)
    assert len(messages) == 4


def test_loop_streams_tool_output_before_tool_finished():
    manifest = echo_manifest()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                text=None,
                tool_calls=[ToolCall("call-1", "echo", {"message": "ping"})],
                raw={"role": "assistant"},
                stop_reason="tool_calls",
            ),
            AssistantTurn(
                text="done",
                tool_calls=[],
                raw={"role": "assistant"},
                stop_reason="end",
            ),
        ]
    )
    loop = AgentLoop(
        provider,
        Broker(manifest, cast(JailBuilder, LocalJail()), PolicyEngine()),
        manifest,
        "system",
    )

    events = asyncio.run(_drain(loop, [{"role": "user", "content": "use echo"}]))

    delta_index = next(
        index
        for index, event in enumerate(events)
        if isinstance(event, ToolOutputDelta)
    )
    finished_index = next(
        index for index, event in enumerate(events) if isinstance(event, ToolFinished)
    )
    assert events[delta_index].text == "ping\n"
    assert delta_index < finished_index


def test_loop_stops_immediately_when_no_tools_requested():
    manifest = echo_manifest()
    provider = ScriptedProvider(
        [
            AssistantTurn(
                text="Hello, no tools needed.",
                tool_calls=[],
                raw={"role": "assistant"},
                stop_reason="end",
            )
        ]
    )
    loop = AgentLoop(
        provider,
        Broker(manifest, cast(JailBuilder, LocalJail()), PolicyEngine()),
        manifest,
        "system",
    )

    messages = [{"role": "user", "content": "hi"}]
    events = asyncio.run(_drain(loop, messages))

    assert _text(events) == "Hello, no tools needed."
    assert provider.received_results == []
    # transcript: user, assistant(final)
    assert len(messages) == 2


def test_cancel_mid_turn_leaves_a_valid_transcript():
    """Cancelling while the first round-trip is still streaming must not commit a
    partial assistant message: the transcript stays exactly the user message."""
    manifest = echo_manifest()

    class HangingProvider(ScriptedProvider):
        async def stream(self, system, messages, tools):
            yield TextDelta("thinking")
            # Never reaches TurnComplete; the consumer cancels mid-stream.
            await asyncio.Event().wait()

    provider = HangingProvider([])
    loop = AgentLoop(
        provider,
        Broker(manifest, cast(JailBuilder, LocalJail()), PolicyEngine()),
        manifest,
        "system",
    )
    messages = [{"role": "user", "content": "hi"}]

    async def run_and_cancel():
        agen = loop.run_turn(messages)
        # Pull the first delta, then abort the generator mid-turn.
        first = await agen.__anext__()
        assert isinstance(first, TextDelta)
        await agen.aclose()

    asyncio.run(run_and_cancel())

    # Nothing partial was committed; only the original user message remains.
    assert messages == [{"role": "user", "content": "hi"}]


def test_aclose_mid_tool_sets_cancellation_and_leaves_transcript():
    """Closing the turn generator while a tool is running must signal the jail
    to terminate instead of leaking the worker or committing a result."""
    manifest = echo_manifest()

    class CancellingBroker:
        def __init__(self):
            self.cancelled = threading.Event()

        def handle(self, call, output_callback=None, cancellation=None):
            # Emit a chunk so the caller can advance past ToolStarted and then
            # close the generator while this worker is still running.
            if output_callback is not None:
                output_callback("partial output")
            # Block the worker until the loop has signalled cancellation, proving
            # the aclose path propagated the cancellation event.
            if cancellation is not None:
                cancellation.wait(timeout=5)
                self.cancelled.set()
            return ToolResult(call.id, "should-not-appear", is_error=False)

    provider = ScriptedProvider(
        [
            AssistantTurn(
                text=None,
                tool_calls=[ToolCall("call-1", "echo", {"message": "ping"})],
                raw={"role": "assistant"},
                stop_reason="tool_calls",
            ),
        ]
    )
    broker = CancellingBroker()
    loop = AgentLoop(provider, cast(Broker, broker), manifest, "system")
    messages = [{"role": "user", "content": "use echo"}]

    async def run_and_cancel():
        agen = loop.run_turn(messages)
        async for event in agen:
            if isinstance(event, ToolOutputDelta):
                await agen.aclose()
                break

    asyncio.run(run_and_cancel())

    # The closing handshake signalled cancellation and did not commit anything.
    assert broker.cancelled.is_set()
    assert messages == [{"role": "user", "content": "use echo"}]


def test_task_cancel_mid_tool_terminates_worker_synchronously():
    """The CLI cancels an in-flight turn with ``task.cancel()`` (not ``aclose``).
    That CancelledError chains through ``__anext__`` into ``_run_tool``'s own
    ``await``, so the jail's cancellation event must be set — and the worker
    torn down — before awaiting the cancelled task returns."""
    manifest = echo_manifest()

    class CancellingBroker:
        def __init__(self):
            self.cancelled = threading.Event()

        def handle(self, call, output_callback=None, cancellation=None):
            if output_callback is not None:
                output_callback("partial output")
            if cancellation is not None:
                cancellation.wait(timeout=5)
                self.cancelled.set()
            return ToolResult(call.id, "should-not-appear", is_error=False)

    provider = ScriptedProvider(
        [
            AssistantTurn(
                text=None,
                tool_calls=[ToolCall("call-1", "echo", {"message": "ping"})],
                raw={"role": "assistant"},
                stop_reason="tool_calls",
            ),
        ]
    )
    broker = CancellingBroker()
    loop = AgentLoop(provider, cast(Broker, broker), manifest, "system")
    messages = [{"role": "user", "content": "use echo"}]

    async def consume(agen, seen):
        async for event in agen:
            seen.append(event)

    async def run_and_cancel():
        agen = loop.run_turn(messages)
        seen: list = []
        task = asyncio.create_task(consume(agen, seen))
        # Wait until output is flowing, then cancel the task as the CLI does.
        while not any(isinstance(e, ToolOutputDelta) for e in seen):
            await asyncio.sleep(0)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Teardown is synchronous on this path: no extra loop pumping needed.
        return broker.cancelled.is_set()

    cancelled_on_return = asyncio.run(run_and_cancel())

    assert cancelled_on_return
    assert messages == [{"role": "user", "content": "use echo"}]
