"""The OpenAI Responses wire (`wire = "responses"`): request body, input items and stream events.

Requests are stateless (`store: false`); encrypted reasoning items are kept in
`Message.reasoning` and sent back so reasoning models can continue a tool loop.
"""

import json
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from forge.providers.base import (
    ChatRequest,
    ImagePart,
    Message,
    StreamItem,
    TextPart,
    ToolCall,
    Usage,
)
from forge.providers.sse import read_events

ERROR_EVENTS = ("error", "response.failed")


def responses_body(req: ChatRequest, minimal: bool) -> dict[str, Any]:
    """The /responses request body; `minimal` leaves out fields lenient servers reject."""
    body: dict[str, Any] = {
        "model": req.model,
        "input": to_input_items(req, minimal),
        "stream": True,
    }
    if req.system:
        body["instructions"] = req.system
    if req.tools:
        body["tools"] = [{"type": "function", **t.model_dump()} for t in req.tools]
    if req.max_output:
        body["max_output_tokens"] = req.max_output
    if req.temperature is not None:
        body["temperature"] = req.temperature
    if not minimal:
        body["store"] = False
        body["include"] = ["reasoning.encrypted_content"]
        if req.reasoning_effort:
            body["reasoning"] = {"effort": req.reasoning_effort}
        if req.json_schema:
            body["text"] = {
                "format": {"type": "json_schema", "name": "result", "schema": req.json_schema}
            }
    return body


def to_input_items(req: ChatRequest, minimal: bool = False) -> list[dict[str, Any]]:
    """Translate internal messages into Responses input items."""
    items: list[dict[str, Any]] = []
    for message in req.messages:
        if message.role == "tool" and message.tool_result:
            result = message.tool_result
            items.append(
                {"type": "function_call_output", "call_id": result.call_id, "output": result.text}
            )
            if result.images:
                items.append(image_input(result.images))
        elif message.role == "assistant":
            items += assistant_items(message, keep_reasoning=not minimal)
        elif message.role != "system":
            items.append({"role": "user", "content": [input_part(p) for p in message.parts]})
    return items


def assistant_items(message: Message, keep_reasoning: bool) -> list[dict[str, Any]]:
    """Reasoning items (if kept), the text, then one function_call item per tool call."""
    items: list[dict[str, Any]] = []
    if keep_reasoning and message.reasoning:
        items += json.loads(message.reasoning)
    if message.text():
        items.append({"role": "assistant", "content": message.text()})
    for call in message.tool_calls:
        raw = call.arguments.get("_raw_arguments")
        arguments = raw if isinstance(raw, str) else json.dumps(call.arguments)
        items.append(
            {"type": "function_call", "call_id": call.id, "name": call.name, "arguments": arguments}
        )
    return items


def input_part(part: TextPart | ImagePart) -> dict[str, Any]:
    """One user content part."""
    if isinstance(part, TextPart):
        return {"type": "input_text", "text": part.text}
    return {"type": "input_image", "image_url": f"data:{part.media_type};base64,{part.data_b64}"}


def image_input(images: list[ImagePart]) -> dict[str, Any]:
    """Tool outputs are text only here, so returned images follow as a user message."""
    parts = [{"type": "input_text", "text": "Images returned by the tool call above:"}]
    return {"role": "user", "content": parts + [input_part(i) for i in images]}


class ResponsesStream:
    """Collects a streamed response: text, finished output items and usage."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.items: list[dict[str, Any]] = []
        self.usage = Usage()

    async def events(self, response: httpx.Response) -> AsyncIterator[StreamItem]:
        """Yield text deltas; raise the stream's error event as an exception payload."""
        async for event in read_events(response):
            if not event.data.strip() or event.data.strip() == "[DONE]":
                continue
            data = json.loads(event.data)
            kind = str(data.get("type") or event.event or "")
            if kind in ERROR_EVENTS:
                raise StreamFailedError(
                    data.get("error") or (data.get("response") or {}).get("error") or data
                )
            if kind == "response.output_text.delta" and data.get("delta"):
                self.text.append(data["delta"])
                yield StreamItem(delta=data["delta"])
            elif kind == "response.output_item.done":
                self.items.append(data["item"])
                if data["item"].get("type") == "function_call":
                    yield StreamItem(tool_call=function_call_of(data["item"], len(self.items) - 1))
            elif kind in ("response.completed", "response.incomplete"):
                self.usage = parse_usage((data.get("response") or {}).get("usage") or {})

    def message(self) -> Message:
        """The finished assistant message."""
        text = "".join(self.text)
        calls = [
            function_call_of(i, n)
            for n, i in enumerate(self.items)
            if i.get("type") == "function_call"
        ]
        reasoning = [i for i in self.items if i.get("type") == "reasoning"]
        return Message(
            role="assistant",
            parts=[TextPart(text=text)] if text else [],
            tool_calls=calls,
            reasoning=json.dumps(reasoning) if reasoning else None,
        )


class StreamFailedError(Exception):
    """An error event inside a Responses stream (mapped by the adapter)."""

    def __init__(self, error: Any) -> None:
        super().__init__(json.dumps(error))
        self.error = error


def function_call_of(item: Mapping[str, Any], n: int) -> ToolCall:
    """A ToolCall from a finished function_call item; unparsable arguments are kept raw."""
    raw = str(item.get("arguments") or "").strip() or "{}"
    try:
        arguments = json.loads(raw)
    except json.JSONDecodeError:
        arguments = None
    if not isinstance(arguments, dict):
        arguments = {"_raw_arguments": raw}
    call_id = str(item.get("call_id") or item.get("id") or f"call_{n}")
    return ToolCall(id=call_id, name=str(item.get("name") or ""), arguments=arguments)


def parse_usage(data: Mapping[str, Any]) -> Usage:
    """Read token counts from a Responses usage object."""
    details = data.get("input_tokens_details") or {}
    return Usage(
        input_tokens=int(data.get("input_tokens") or 0),
        output_tokens=int(data.get("output_tokens") or 0),
        cached_tokens=int(details.get("cached_tokens") or 0),
    )
