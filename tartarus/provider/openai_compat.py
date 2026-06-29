"""OpenAI-compatible chat-completions provider.

Targets POST {base_url}/chat/completions, which serves OpenCode Zen, OpenAI,
Together, local Ollama/llama.cpp/vLLM, and most gateways. Uses raw HTTP via httpx
so any compatible base URL works without coupling to a vendor SDK. Swapping
backends is purely config (base_url / api_key / model); no code changes.
"""

import json
from collections.abc import AsyncIterator, Mapping

import httpx

from tartarus.models import (
    AssistantTurn,
    StreamEvent,
    TextDelta,
    ToolCall,
    TurnComplete,
)

DEFAULT_REQUEST_TIMEOUT_SECONDS = 120

# Map the backend's finish_reason onto our normalized stop_reason vocabulary.
_FINISH_REASON_TO_STOP = {
    "tool_calls": "tool_calls",
    "stop": "end",
    "length": "length",
}


class ProviderError(Exception):
    """Raised when the backend is unreachable or returns an unusable response."""


class OpenAICompatProvider:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        max_tokens: int,
        extra_headers: dict[str, str] | None = None,
        sampling: Mapping[str, object] | None = None,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
    ):
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        # Generation knobs (temperature, top_p, ...) passed through to the request
        # body under their provider-native names. None/empty means backend defaults.
        self._sampling = dict(sampling or {})
        self._timeout = timeout
        self._headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            **(extra_headers or {}),
        }

    def adapt_tools(self, tools: list[dict]) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool["description"],
                    "parameters": tool["parameters"],
                },
            }
            for tool in tools
        ]

    async def complete(
        self, system: str, messages: list[dict], tools: list[dict]
    ) -> AssistantTurn:
        response = await self._post_chat_completions(
            self._build_body(system, messages, tools)
        )
        return self._parse_turn(response)

    async def stream(
        self, system: str, messages: list[dict], tools: list[dict]
    ) -> AsyncIterator[StreamEvent]:
        """Stream a turn over SSE, yielding TextDelta then a final TurnComplete.

        Text chunks are yielded as they arrive; tool-call fragments (which the
        backend splits across chunks by index) are reassembled, then finalized
        through the same helpers `complete` uses so the assembled turn is
        identical in shape to the non-streaming path.
        """
        body = self._build_body(system, messages, tools, stream=True)
        url = f"{self._base_url}/chat/completions"

        text_parts: list[str] = []
        # index -> {"id", "name", "arguments"} accumulated across delta fragments.
        tool_fragments: dict[int, dict] = {}
        finish_reason: str | None = None

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                async with client.stream(
                    "POST", url, headers=self._headers, json=body
                ) as response:
                    if response.status_code >= 400:
                        detail = (await response.aread()).decode(errors="replace")
                        raise ProviderError(
                            f"backend returned HTTP {response.status_code}: {detail}"
                        )
                    async for line in response.aiter_lines():
                        data = self._sse_data(line)
                        if data is None:
                            continue
                        if data == "[DONE]":
                            break
                        choice = self._stream_choice(data)
                        if choice is None:
                            continue
                        delta = choice.get("delta") or {}
                        content = delta.get("content")
                        if content:
                            text_parts.append(content)
                            yield TextDelta(content)
                        self._accumulate_tool_calls(
                            delta.get("tool_calls") or [], tool_fragments
                        )
                        if choice.get("finish_reason"):
                            finish_reason = choice["finish_reason"]
        except httpx.RequestError as error:
            raise ProviderError(f"could not reach backend at {url}: {error}") from error

        yield TurnComplete(
            self._assemble_turn(text_parts, tool_fragments, finish_reason)
        )

    def assistant_message(self, turn: AssistantTurn) -> dict:
        # Append verbatim (including its tool_calls) so the transcript stays valid.
        return turn.raw

    def tool_result_messages(self, results: list) -> list[dict]:
        return [
            {
                "role": "tool",
                "tool_call_id": result.call_id,
                "content": result.output,
            }
            for result in results
        ]

    # --- internals -------------------------------------------------------------

    def _build_body(
        self, system: str, messages: list[dict], tools: list[dict], stream: bool = False
    ) -> dict:
        body = {
            **self._sampling,
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": [{"role": "system", "content": system}, *messages],
        }
        if stream:
            body["stream"] = True
        if tools:
            body["tools"] = self.adapt_tools(tools)
            body["tool_choice"] = "auto"
        return body

    @staticmethod
    def _sse_data(line: str) -> str | None:
        """Return the payload of an SSE `data:` line, or None for blanks/comments."""
        line = line.strip()
        if not line or not line.startswith("data:"):
            return None
        return line[len("data:") :].strip()

    @staticmethod
    def _stream_choice(data: str) -> dict | None:
        """Parse one SSE chunk's JSON and return its first choice, if any.

        Invalid JSON is treated as a provider error rather than silently dropped,
        so a malfunctioning backend cannot be mistaken for an empty stream.
        """
        try:
            chunk = json.loads(data)
        except json.JSONDecodeError as error:
            raise ProviderError(f"backend sent invalid JSON: {error}") from error

        if not isinstance(chunk, dict):
            raise ProviderError("backend sent a non-object SSE chunk")
        choices = chunk.get("choices")
        if not isinstance(choices, list) or not choices:
            return None
        return choices[0]

    @staticmethod
    def _accumulate_tool_calls(deltas: list[dict], fragments: dict[int, dict]) -> None:
        """Merge streamed tool-call fragments into per-index accumulators.

        The backend streams each tool call across several chunks: the first
        carries `id` and `function.name`, later ones append `function.arguments`
        string fragments. We key by `index` and concatenate.
        """
        for delta in deltas:
            index = delta.get("index", 0)
            slot = fragments.setdefault(index, {"id": "", "name": "", "arguments": ""})
            if delta.get("id"):
                slot["id"] = delta["id"]
            function = delta.get("function") or {}
            if function.get("name"):
                slot["name"] = function["name"]
            if function.get("arguments"):
                slot["arguments"] += function["arguments"]

    def _assemble_turn(
        self,
        text_parts: list[str],
        tool_fragments: dict[int, dict],
        finish_reason: str | None,
    ) -> AssistantTurn:
        """Finalize accumulated stream state into an AssistantTurn.

        Uses the same `_parse_arguments` / `_normalize_stop_reason` helpers as the
        non-streaming path and rebuilds the assistant `raw` message so the
        transcript stays valid (tool_calls in OpenAI's native shape).
        """
        text = "".join(text_parts) or None
        tool_calls = []
        raw_tool_calls = []
        for index in sorted(tool_fragments):
            slot = tool_fragments[index]
            arguments, argument_error = self._parse_arguments(slot["arguments"])
            tool_calls.append(
                ToolCall(
                    id=slot["id"],
                    name=slot["name"],
                    arguments=arguments,
                    argument_error=argument_error,
                )
            )
            raw_tool_calls.append(
                {
                    "id": slot["id"],
                    "type": "function",
                    "function": {"name": slot["name"], "arguments": slot["arguments"]},
                }
            )

        raw = {"role": "assistant", "content": text}
        if raw_tool_calls:
            raw["tool_calls"] = raw_tool_calls

        return AssistantTurn(
            text=text,
            tool_calls=tool_calls,
            raw=raw,
            stop_reason=self._normalize_stop_reason(finish_reason, tool_calls),
        )

    async def _post_chat_completions(self, body: dict) -> dict:
        url = f"{self._base_url}/chat/completions"
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, headers=self._headers, json=body)
        except httpx.RequestError as error:
            raise ProviderError(f"could not reach backend at {url}: {error}") from error

        if response.status_code >= 400:
            raise ProviderError(
                f"backend returned HTTP {response.status_code}: {response.text}"
            )
        return response.json()

    def _parse_turn(self, response: dict) -> AssistantTurn:
        choices = response.get("choices")
        if not choices:
            raise ProviderError("response contained no choices")

        choice = choices[0]
        message = choice.get("message", {})
        tool_calls = self._parse_tool_calls(message.get("tool_calls") or [])
        stop_reason = self._normalize_stop_reason(
            choice.get("finish_reason"), tool_calls
        )
        return AssistantTurn(
            text=message.get("content"),
            tool_calls=tool_calls,
            raw=message,
            stop_reason=stop_reason,
        )

    def _parse_tool_calls(self, raw_tool_calls: list[dict]) -> list[ToolCall]:
        calls = []
        for raw in raw_tool_calls:
            function = raw.get("function", {})
            arguments, argument_error = self._parse_arguments(function.get("arguments"))
            calls.append(
                ToolCall(
                    id=raw.get("id", ""),
                    name=function.get("name", ""),
                    arguments=arguments,
                    argument_error=argument_error,
                )
            )
        return calls

    @staticmethod
    def _parse_arguments(
        raw_arguments: str | None,
    ) -> tuple[dict, str | None]:
        """Parse the JSON-string arguments, reporting malformed JSON as an error."""
        if raw_arguments in (None, ""):
            return {}, None
        try:
            parsed = json.loads(raw_arguments)
        except json.JSONDecodeError as error:
            return {}, f"arguments were not valid JSON: {error}"
        if not isinstance(parsed, dict):
            return {}, "arguments must be a JSON object"
        return parsed, None

    @staticmethod
    def _normalize_stop_reason(
        finish_reason: str | None, tool_calls: list[ToolCall]
    ) -> str:
        if tool_calls:
            return "tool_calls"
        mapped = _FINISH_REASON_TO_STOP.get(finish_reason, "error")
        # A finish_reason of "tool_calls" with no parsed calls is degenerate;
        # treat it as a normal end so the loop does not spin.
        return "end" if mapped == "tool_calls" else mapped
