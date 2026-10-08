"""The model gateway: run tokens, what passes, whose key pays, limits and metering."""

import json
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from forge_web.db.engine import Database
from forge_web.db.models import Chat, KeyGrant, Project, UsageRecord, User
from forge_web.gateway.keys import save_key
from forge_web.gateway.meter import UsageSniffer, cost, price
from forge_web.gateway.proxy import Gateway
from forge_web.gateway.tokens import parse_token, run_token
from forge_web.gateway.upstreams import allowed_path, incoming_token, model_of, prepare_body
from forge_web.settings import GatewaySettings
from forge_web.vault import Vault

ANTHROPIC_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100,'
    '"cache_read_input_tokens":20,"output_tokens":1}}}\n\n'
    'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
    '"delta":{"type":"text_delta","text":"Hello"}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":50}}\n\n'
)


def test_run_tokens_are_signed() -> None:
    token = run_token(b"k" * 32, "chat1", 3)
    assert parse_token(b"k" * 32, token) == ("chat1", 3)
    assert parse_token(b"x" * 32, token) is None
    assert parse_token(b"k" * 32, token.replace(".3.", ".4.")) is None
    assert parse_token(b"k" * 32, "sk-ant-real-key") is None


def test_only_model_calls_pass() -> None:
    assert allowed_path("anthropic", "POST", "v1/messages")
    assert allowed_path("anthropic", "POST", "v1/messages/count_tokens")
    assert not allowed_path("anthropic", "GET", "v1/messages")
    assert not allowed_path("anthropic", "POST", "v1/files")
    assert allowed_path("openai_compat", "POST", "chat/completions")
    assert not allowed_path("openai_compat", "POST", "fine_tuning/jobs")
    assert allowed_path("google", "POST", "v1beta/models/gemini-2.5-pro:streamGenerateContent")
    assert not allowed_path("google", "POST", "v1beta/cachedContents")


def test_token_is_found_wherever_the_sdk_puts_it() -> None:
    assert incoming_token({"authorization": "Bearer abc"}, {}) == "abc"
    assert incoming_token({"x-api-key": "abc"}, {}) == "abc"
    assert incoming_token({"x-goog-api-key": "abc"}, {}) == "abc"
    assert incoming_token({}, {"key": "abc"}) == "abc"


def test_requests_are_capped_and_asked_for_usage() -> None:
    body: dict[str, Any] = {"model": "gpt-5", "max_tokens": 900_000, "stream": True}
    assert prepare_body("openai_compat", "chat/completions", body, 64_000) == 64_000
    assert body["max_tokens"] == 64_000 and body["stream_options"] == {"include_usage": True}
    google: dict[str, Any] = {}
    prepare_body("google", "v1beta/models/x:generateContent", google, 1000)
    assert google["generationConfig"]["maxOutputTokens"] == 1000
    path = "v1beta/models/gemini-2.5-pro:streamGenerateContent"
    assert model_of("google", path, {}) == "gemini-2.5-pro"


def test_usage_is_read_from_each_provider_format() -> None:
    sniffer = UsageSniffer("anthropic")
    for piece in (ANTHROPIC_SSE[:40], ANTHROPIC_SSE[40:]):  # split mid-line on purpose
        sniffer.feed(piece.encode())
    counted = sniffer.finish()
    assert (counted.input_tokens, counted.output_tokens, counted.reported) == (120, 50, True)
    openai = UsageSniffer("openai_compat")
    openai.feed(b'data: {"choices":[{"delta":{"content":"hi"}}]}\n\n')
    openai.feed(b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3}}\n\n')
    assert (openai.finish().input_tokens, openai.finish().output_tokens) == (7, 3)
    responses = UsageSniffer("openai_compat")
    responses.feed(
        b'data: {"type":"response.completed",'
        b'"response":{"usage":{"input_tokens":9,"output_tokens":4}}}\n'
    )
    assert responses.finish().output_tokens == 4
    google = UsageSniffer("google")
    google.feed(b'data: {"usageMetadata":{"promptTokenCount":11,"candidatesTokenCount":5}}\n\n')
    assert google.finish().input_tokens == 11
    plain = UsageSniffer("anthropic")
    plain.feed(b'{"usage": {"input_tokens": 3, "output_tokens": 2}}')
    assert plain.finish().output_tokens == 2


def test_prices_come_from_the_catalog_with_a_fallback() -> None:
    assert price("anthropic", "claude-sonnet-5-5") == (2.0, 10.0)
    assert cost("anthropic", "claude-sonnet-5-5", 1_000_000, 100_000) == pytest.approx(3.0)
    assert price("openai", "a-model-nobody-knows") == (5.0, 25.0)


# The gateway app ------------------------------------------------------------------------------


class Chunks(httpx.AsyncByteStream):
    """A response body that arrives in pieces, like a real stream."""

    def __init__(self, data: bytes) -> None:
        self.data = data

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.data), 64):
            yield self.data[start : start + 64]


class Upstream:
    """A stand-in for a provider API that records what it received."""

    def __init__(self, body: str = ANTHROPIC_SSE, status: int = 200) -> None:
        self.requests: list[httpx.Request] = []
        self.body, self.status = body, status

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        headers = {"content-type": "text/event-stream"}
        return httpx.Response(self.status, stream=Chunks(self.body.encode()), headers=headers)


@pytest.fixture
async def world(tmp_path: Path) -> AsyncIterator[dict[str, Any]]:
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'g.db'}")
    await db.migrate()
    now = time.time()
    async with db.session() as session, session.begin():
        session.add_all(
            [
                User(id="u1", email="a@x", role="member", status="active", created_at=now),
                User(id="admin", email="b@x", role="admin", status="active", created_at=now),
            ]
        )
        await session.flush()
        session.add(Project(id="p1", name="P", owner_id="u1", created_at=now, updated_at=now))
        await session.flush()
        session.add_all(
            [
                Chat(id="c1", project_id="p1", user_id="u1", created_at=now, updated_at=now),
                Chat(id="c2", project_id="p1", user_id="admin", created_at=now, updated_at=now),
            ]
        )
    working = {"c1", "c2"}
    vault = Vault(b"m" * 32)
    gateway = Gateway(db, vault, GatewaySettings(monthly_limit_usd=1.0), lambda c: c in working)
    upstream = Upstream()
    gateway.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=gateway.app()), base_url="http://gw"
    )
    yield {"db": db, "vault": vault, "gateway": gateway, "upstream": upstream, "client": client,
           "working": working}  # fmt: skip
    await client.aclose()
    await gateway.close()
    await db.close()


def token(world: dict[str, Any], chat_id: str = "c1", generation: int = 1) -> str:
    return run_token(world["gateway"].key, chat_id, generation)


async def call(
    world: dict[str, Any], token_value: str, path: str = "anthropic/v1/messages", **body: Any
) -> httpx.Response:
    payload = {"model": "claude-sonnet-5-5", "max_tokens": 1000, "messages": [], **body}
    return await world["client"].post(f"/{path}", json=payload, headers={"x-api-key": token_value})


async def usage_rows(world: dict[str, Any]) -> list[UsageRecord]:
    await world["gateway"].close()  # waits for recording
    world["gateway"]._settling = set()
    async with world["db"].session() as session:
        return list(await session.scalars(select(UsageRecord)))


async def test_tokens_must_be_genuine_current_and_working(world: dict[str, Any]) -> None:
    assert (await call(world, "nonsense")).status_code == 401
    assert (await call(world, token(world, generation=2))).status_code == 401
    world["working"].discard("c1")
    refused = await call(world, token(world))
    assert refused.status_code == 401 and "only work while" in refused.json()["error"]["message"]
    assert world["upstream"].requests == []


async def test_without_a_key_nothing_is_forwarded(world: dict[str, Any]) -> None:
    refused = await call(world, token(world))
    assert refused.status_code == 403 and "No API key" in refused.json()["error"]["message"]
    assert world["upstream"].requests == []


async def test_own_key_is_swapped_in_and_usage_recorded(world: dict[str, Any]) -> None:
    await save_key(world["db"], world["vault"], "u1", "anthropic", "sk-ant-real-key-123456")
    response = await call(world, token(world))
    assert response.status_code == 200 and "Hello" in response.text
    sent = world["upstream"].requests[0]
    assert sent.headers["x-api-key"] == "sk-ant-real-key-123456"
    assert (
        token(world) not in str(sent.headers)
        and str(sent.url) == "https://api.anthropic.com/v1/messages"
    )
    rows = await usage_rows(world)
    assert [(r.key_kind, r.input_tokens, r.output_tokens, r.estimated) for r in rows] == [
        ("own", 120, 50, False)
    ]


async def test_server_keys_need_a_grant_and_respect_the_limit(world: dict[str, Any]) -> None:
    await save_key(world["db"], world["vault"], "", "anthropic", "sk-ant-server-key-0001")
    assert (await call(world, token(world))).status_code == 403  # member without a grant
    assert (await call(world, token(world, "c2"))).status_code == 200  # admins may
    async with world["db"].session() as session, session.begin():
        session.add(KeyGrant(user_id="u1", allowed=True, monthly_limit_usd=0.0))
    limited = await call(world, token(world))
    assert limited.status_code == 403 and "monthly limit" in limited.json()["error"]["message"]
    async with world["db"].session() as session, session.begin():
        grant = await session.get(KeyGrant, "u1")
        assert grant is not None
        grant.monthly_limit_usd = 5.0
    assert (await call(world, token(world))).status_code == 200
    rows = await usage_rows(world)
    assert {r.user_id for r in rows} == {"admin", "u1"} and all(
        r.key_kind == "server" for r in rows
    )


async def test_other_endpoints_are_refused(world: dict[str, Any]) -> None:
    await save_key(world["db"], world["vault"], "u1", "anthropic", "sk-ant-real-key-123456")
    assert (await call(world, token(world), "anthropic/v1/files")).status_code == 403
    assert (await call(world, token(world), "litellm/v1/messages")).status_code == 404
    assert world["upstream"].requests == []


async def test_requests_are_capped_on_the_way(world: dict[str, Any]) -> None:
    await save_key(world["db"], world["vault"], "u1", "anthropic", "sk-ant-real-key-123456")
    world["gateway"].settings.max_output_tokens = 500
    await call(world, token(world), max_tokens=100_000)
    assert json.loads(world["upstream"].requests[0].content)["max_tokens"] == 500


async def test_a_stream_without_usage_is_charged_an_estimate(world: dict[str, Any]) -> None:
    await save_key(world["db"], world["vault"], "u1", "anthropic", "sk-ant-real-key-123456")
    world["upstream"].body = (
        'data: {"type":"content_block_delta","delta":{"text":"' + "x" * 300 + '"}}\n'
    )
    await call(world, token(world))
    rows = await usage_rows(world)
    assert rows[0].estimated and rows[0].output_tokens == 100


async def test_upstream_errors_cost_nothing(world: dict[str, Any]) -> None:
    await save_key(world["db"], world["vault"], "u1", "anthropic", "sk-ant-real-key-123456")
    world["upstream"].status, world["upstream"].body = 529, '{"type":"error"}'
    response = await call(world, token(world))
    assert response.status_code == 529
    rows = await usage_rows(world)
    assert rows[0].cost_usd == 0 and rows[0].output_tokens == 0
