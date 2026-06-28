import asyncio

from typing import Any

import pytest

from tartarus.models import TextDelta, ToolResult, TurnComplete
from tartarus.provider.openai_compat import OpenAICompatProvider, ProviderError
from tests.manifest_fixtures import echo_manifest


def _provider(**overrides: Any):
    kwargs: dict[str, Any] = dict(
        base_url="https://example.test/v1",
        api_key="secret",
        model="opencode/gpt-5.5",
        max_tokens=128,
    )
    kwargs.update(overrides)
    return OpenAICompatProvider(**kwargs)


# -- shared fake HTTP stack ---------------------------------------------------


class _FakeTransport:
    """Replay supplied SSE lines as a streaming response."""

    def __init__(self, lines: list[str], status_code: int = 200):
        self._lines = lines
        self.status_code = status_code

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b"error body"


_stored_stream: _FakeTransport = _FakeTransport([])


class _FakeClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, *args, **kwargs):
        return _make_stream(_stored_stream)


def _make_stream(transport):
    class _Stream:
        def __init__(self):
            pass

        async def __aenter__(self):
            return transport

        async def __aexit__(self, *exc):
            return False

    return _Stream()


def _configure_fake_stream(lines, status_code=200):
    global _stored_stream
    _stored_stream = _FakeTransport(lines, status_code)


def test_adapt_tools_wraps_in_function_envelope():
    adapted = _provider().adapt_tools(echo_manifest().tools)

    assert adapted[0]["type"] == "function"
    assert adapted[0]["function"]["name"] == "echo"
    assert "parameters" in adapted[0]["function"]


def test_build_body_includes_sampling_when_set():
    provider = _provider(sampling={"temperature": 0, "top_p": 0.9})

    body = provider._build_body("sys", [], [])

    assert body["temperature"] == 0
    assert body["top_p"] == 0.9


def test_build_body_omits_sampling_when_unset():
    body = _provider()._build_body("sys", [], [])

    assert "temperature" not in body
    assert "top_p" not in body


def test_build_body_sampling_cannot_override_reserved_fields():
    provider = _provider(
        sampling={"model": "other", "max_tokens": 999, "messages": []},
    )

    body = provider._build_body("sys", [{"role": "user", "content": "hi"}], [])

    assert body["model"] == "opencode/gpt-5.5"
    assert body["max_tokens"] == 128
    assert body["messages"] == [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
    ]


def test_parse_turn_reads_tool_calls():
    response = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call-9",
                            "function": {
                                "name": "echo",
                                "arguments": '{"message": "hi"}',
                            },
                        }
                    ],
                },
            }
        ]
    }

    turn = _provider()._parse_turn(response)

    assert turn.stop_reason == "tool_calls"
    assert turn.tool_calls[0].name == "echo"
    assert turn.tool_calls[0].arguments == {"message": "hi"}
    assert turn.tool_calls[0].argument_error is None


def test_parse_turn_flags_malformed_arguments():
    response = {
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "tool_calls": [
                        {
                            "id": "call-9",
                            "function": {"name": "echo", "arguments": "{not json"},
                        }
                    ]
                },
            }
        ]
    }

    turn = _provider()._parse_turn(response)

    assert turn.tool_calls[0].argument_error is not None
    assert turn.tool_calls[0].arguments == {}


def test_normalize_stop_reason_maps_finish_reasons():
    response = {"choices": [{"finish_reason": "stop", "message": {"content": "done"}}]}

    turn = _provider()._parse_turn(response)

    assert turn.stop_reason == "end"
    assert turn.text == "done"


def test_tool_result_messages_use_tool_role():
    messages = _provider().tool_result_messages(
        [ToolResult(call_id="call-9", output="hi", is_error=False)]
    )

    assert messages == [{"role": "tool", "tool_call_id": "call-9", "content": "hi"}]


# --- streaming ----------------------------------------------------------------


def test_sse_data_extracts_payload_and_skips_noise():
    provider = _provider()

    assert provider._sse_data('data: {"a":1}') == '{"a":1}'
    assert provider._sse_data("data: [DONE]") == "[DONE]"
    assert provider._sse_data("") is None
    assert provider._sse_data(": comment") is None


def test_accumulate_tool_calls_reassembles_fragments_by_index():
    provider = _provider()
    fragments = {}
    # First chunk carries id + name; later chunks append argument fragments.
    provider._accumulate_tool_calls(
        [{"index": 0, "id": "call-1", "function": {"name": "echo"}}], fragments
    )
    provider._accumulate_tool_calls(
        [{"index": 0, "function": {"arguments": '{"mess'}}], fragments
    )
    provider._accumulate_tool_calls(
        [{"index": 0, "function": {"arguments": 'age": "hi"}'}}], fragments
    )

    assert fragments[0] == {
        "id": "call-1",
        "name": "echo",
        "arguments": '{"message": "hi"}',
    }


def test_assemble_turn_finalizes_text_and_tool_calls():
    provider = _provider()
    fragments = {0: {"id": "call-1", "name": "echo", "arguments": '{"message": "hi"}'}}

    turn = provider._assemble_turn(["Hel", "lo"], fragments, "tool_calls")

    assert turn.text == "Hello"
    assert turn.stop_reason == "tool_calls"
    assert turn.tool_calls[0].name == "echo"
    assert turn.tool_calls[0].arguments == {"message": "hi"}
    # raw must carry tool_calls in OpenAI's native shape for transcript validity.
    assert turn.raw["tool_calls"][0]["function"]["name"] == "echo"


def test_assemble_turn_flags_malformed_streamed_arguments():
    provider = _provider()
    fragments = {0: {"id": "c", "name": "echo", "arguments": "{not json"}}

    turn = provider._assemble_turn([], fragments, "tool_calls")

    assert turn.tool_calls[0].argument_error is not None
    assert turn.tool_calls[0].arguments == {}


def test_assemble_turn_maps_plain_finish_reason():
    turn = _provider()._assemble_turn(["done"], {}, "stop")

    assert turn.stop_reason == "end"
    assert turn.text == "done"
    assert turn.tool_calls == []
    assert "tool_calls" not in turn.raw


def test_stream_yields_text_deltas_then_turn_complete(monkeypatch):
    """End-to-end over a fake SSE body: text streams as TextDelta, ending in a
    single TurnComplete with the assembled turn."""
    provider = _provider()

    chunks = [
        'data: {"choices":[{"delta":{"content":"Hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}]}',
        'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}',
        "data: [DONE]",
    ]

    _configure_fake_stream(chunks)
    monkeypatch.setattr(
        "tartarus.provider.openai_compat.httpx.AsyncClient", _FakeClient
    )

    async def collect():
        return [e async for e in provider.stream("sys", [], [])]

    events = asyncio.run(collect())

    assert [e.text for e in events if isinstance(e, TextDelta)] == ["Hel", "lo"]
    assert isinstance(events[-1], TurnComplete)
    assert events[-1].turn.text == "Hello"
    assert events[-1].turn.stop_reason == "end"


@pytest.mark.parametrize(
    "bad_line,expected_msg",
    [
        ("data: not json", "backend sent invalid JSON"),
        ("data: [1, 2, 3]", "backend sent a non-object SSE chunk"),
    ],
)
def test_stream_raises_on_bad_sse_chunk(monkeypatch, bad_line, expected_msg):
    """Invalid JSON or non-object SSE payloads abort the stream with ProviderError."""
    provider = _provider()

    _configure_fake_stream([bad_line])
    monkeypatch.setattr(
        "tartarus.provider.openai_compat.httpx.AsyncClient", _FakeClient
    )

    async def collect():
        return [e async for e in provider.stream("sys", [], [])]

    with pytest.raises(ProviderError, match=expected_msg):
        asyncio.run(collect())


def test_stream_raises_on_http_error(monkeypatch):
    """A non-200 response body aborts the stream with ProviderError."""
    provider = _provider()

    _configure_fake_stream([], status_code=500)
    monkeypatch.setattr(
        "tartarus.provider.openai_compat.httpx.AsyncClient", _FakeClient
    )

    async def collect():
        return [e async for e in provider.stream("sys", [], [])]

    with pytest.raises(ProviderError, match="backend returned HTTP 500"):
        asyncio.run(collect())


def test_stream_passes_tool_call_deltas(monkeypatch):
    """SSE chunks with tool_calls deltas are accumulated into the final turn."""
    provider = _provider()

    chunks = [
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"c1","function":{"name":"echo"}}]}}]}',  # noqa: E501
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"{\\"mess"}}]}}]}',  # noqa: E501
        'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"age\\": \\"hi\\"}"}}]}}]}',  # noqa: E501
        'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
        "data: [DONE]",
    ]

    _configure_fake_stream(chunks)
    monkeypatch.setattr(
        "tartarus.provider.openai_compat.httpx.AsyncClient", _FakeClient
    )

    async def collect():
        return [e async for e in provider.stream("sys", [], [])]

    events = asyncio.run(collect())

    assert isinstance(events[-1], TurnComplete)
    turn = events[-1].turn
    assert turn.stop_reason == "tool_calls"
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].name == "echo"
    assert turn.tool_calls[0].arguments == {"message": "hi"}


def test_complete_round_trips(monkeypatch):
    """The non-streaming complete() method parses a response into an AssistantTurn."""
    provider = _provider()

    response_json = {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"content": "Hello, world"},
            }
        ]
    }

    class _FakePostResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return response_json

    class _FakePostClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, *args, **kwargs):
            return _FakePostResponse()

    monkeypatch.setattr(
        "tartarus.provider.openai_compat.httpx.AsyncClient", _FakePostClient
    )

    turn = asyncio.run(provider.complete("sys", [], []))
    assert turn.text == "Hello, world"
    assert turn.stop_reason == "end"
    assert turn.tool_calls == []
