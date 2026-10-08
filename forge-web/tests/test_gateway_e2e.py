"""End to end: Forge's real Anthropic provider in a sandbox worker talks to a model through the
gateway; the real key is used upstream and never reaches the sandbox."""

import asyncio
import json
import sys
import threading
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse

from support import TRIVIAL_SPEC, LiveServer, dev_settings

REAL_KEY = "sk-ant-test-real-key-0123456789"


def anthropic_events(text: str) -> list[str]:
    message = {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
               "content": [], "stop_reason": None, "stop_sequence": None,
               "usage": {"input_tokens": 321, "output_tokens": 1}}  # fmt: skip
    events = [
        ("message_start", {"type": "message_start", "message": message}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn",
                           "stop_sequence": None}, "usage": {"output_tokens": 77}}),
        ("message_stop", {"type": "message_stop"}),
    ]  # fmt: skip
    return [f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events]


class FakeAnthropic:
    """An Anthropic-compatible API on a free port that answers every call with the task spec."""

    def __init__(self) -> None:
        self.seen: list[dict[str, Any]] = []
        app = FastAPI()

        @app.post("/v1/messages")
        async def messages(request: Request) -> StreamingResponse:
            self.seen.append({"headers": dict(request.headers), "body": await request.json()})

            async def stream() -> AsyncIterator[str]:
                for event in anthropic_events(TRIVIAL_SPEC):
                    yield event

            return StreamingResponse(stream(), media_type="text/event-stream")

        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", loop="asyncio")
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "FakeAnthropic":
        self.thread.start()
        while not self.server.started:
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(10)


@pytest.fixture
def upstream() -> Iterator[FakeAnthropic]:
    with FakeAnthropic() as fake:
        yield fake


def worker_environments() -> list[dict[str, str]]:
    """Environments of running chat workers (Linux: read from /proc)."""
    found = []
    for proc in Path("/proc").iterdir():
        try:
            cmdline = (proc / "cmdline").read_bytes()
            if b"forge_sandbox\x00worker" not in cmdline:
                continue
            raw = (proc / "environ").read_bytes()
        except OSError:
            continue
        found.append(
            dict(e.split("=", 1) for e in raw.decode(errors="replace").split("\0") if "=" in e)
        )
    return found


async def test_forge_reaches_the_model_only_through_the_gateway(
    tmp_path: Path, upstream: FakeAnthropic
) -> None:
    settings = dev_settings(tmp_path / "data")
    settings.dev.fake = False
    settings.gateway.upstreams = {"anthropic": upstream.url}
    with LiveServer(settings) as server:
        headers = server.headers()
        async with httpx.AsyncClient(base_url=server.url, headers=headers, timeout=120) as api:
            saved = await api.post("/api/keys", json={"provider": "anthropic", "key": REAL_KEY})
            assert saved.status_code == 201 and REAL_KEY not in saved.text
            project = (await api.post("/api/projects", json={"name": "Gateway"})).json()
            model = {"model": "anthropic/claude-sonnet-5-5"}
            chat = (await api.post(f"/api/projects/{project['id']}/chats", json=model)).json()
            await api.post(f"/api/chats/{chat['id']}/messages", json={"text": "say hi"})
            environments: list[dict[str, str]] = []
            for _ in range(600):
                if sys.platform.startswith("linux") and not environments:
                    environments = worker_environments()
                if (await api.get(f"/api/chats/{chat['id']}")).json()["state"] == "idle":
                    break
                await asyncio.sleep(0.1)
            events = (await api.get(f"/api/chats/{chat['id']}/events")).json()["items"]
            turns = [e["item"] for e in events if e["item"]["type"] == "turn"]
            assert turns and turns[-1]["ok"], events[-3:]
            usage = (await api.get("/api/usage")).json()
    assert upstream.seen and all(s["headers"]["x-api-key"] == REAL_KEY for s in upstream.seen)
    assert all(s["body"]["model"] == "claude-sonnet-5-5" for s in upstream.seen)
    assert REAL_KEY not in json.dumps(events)
    assert usage["by_model"][0]["output_tokens"] == 77 * len(upstream.seen)
    if sys.platform.startswith("linux"):
        assert environments, "no worker process was seen"
        for env in environments:
            assert REAL_KEY not in json.dumps(env)
            assert env.get("FW_GATEWAY_TOKEN", "").startswith("fwg.")
