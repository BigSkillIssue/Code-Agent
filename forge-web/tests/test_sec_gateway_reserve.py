"""SEC-W2: a reservation on the server's keys is a true upper bound of what a call can cost.

One completion per call, a token cap always in the body, no server-side tools the meter cannot
price, no routing to another model than the one priced, no input stored upstream.
"""

import json
from typing import Any

import httpx
import pytest

from forge_web.gateway.keys import save_key
from forge_web.gateway.meter import WEB_SEARCH_USD, UsageSniffer, cost
from forge_web.gateway.policy import one_completion
from forge_web.gateway.upstreams import prepare_body
from test_gateway import call, token, usage_rows, world  # noqa: F401 (a fixture)

OPENAI_JSON = json.dumps(
    {
        "choices": [{"message": {"content": "hi"}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
)
SEARCH_SSE = (
    'event: message_start\ndata: {"type":"message_start","message":{"usage":{"input_tokens":100,'
    '"output_tokens":1}}}\n\n'
    'event: message_delta\ndata: {"type":"message_delta","usage":{"output_tokens":50,'
    '"server_tool_use":{"web_search_requests":3}}}\n\n'
)


async def post(
    env: dict[str, Any], path: str, body: dict[str, Any], who: str = "c2"
) -> httpx.Response:
    """A call with the admin's chat (allowed the server's keys) unless `who` says otherwise."""
    return await env["client"].post(
        f"/{path}", json=body, headers={"authorization": f"Bearer {token(env, who)}"}
    )


def reservations(env: dict[str, Any]) -> list[float]:
    """Record what the gateway reserves."""
    ledger, held = env["gateway"].ledger, []
    original = ledger.reserve

    async def reserve(user_id: str, amount: float, *args: Any) -> str:
        held.append(amount)
        return str(await original(user_id, amount, *args))

    ledger.reserve = reserve
    return held


def sent(env: dict[str, Any]) -> dict[str, Any]:
    return dict(json.loads(env["upstream"].requests[-1].content))


# Problem 1: one completion, and a cap in every body ----------------------------------------


def test_more_than_one_completion_is_refused() -> None:
    assert one_completion("openai_compat", {"n": 128})
    assert one_completion("openai_compat", {"n": "128"})
    assert one_completion("openai_compat", {"best_of": 4})
    assert one_completion("google", {"generationConfig": {"candidateCount": 8}})
    assert one_completion("google", {"generationConfig": {"candidate_count": 8}})
    assert one_completion("google", {"generation_config": {"max_output_tokens": 10**9}})
    assert not one_completion("openai_compat", {"n": 1})
    assert not one_completion("google", {"generationConfig": {"candidateCount": 1}})
    assert not one_completion("anthropic", {"n": 5})  # not a field Anthropic knows


def test_openai_style_bodies_always_carry_the_cap() -> None:
    chat: dict[str, Any] = {"model": "gpt-5", "messages": []}
    openai = "https://api.openai.com/v1"
    assert prepare_body("openai_compat", "chat/completions", chat, 1000, openai) == 1000
    assert chat["max_completion_tokens"] == 1000
    other: dict[str, Any] = {"model": "deepseek-chat"}
    assert prepare_body("openai_compat", "chat/completions", other, 1000) == 1000
    assert other["max_tokens"] == 1000
    responses: dict[str, Any] = {"model": "gpt-5"}
    assert prepare_body("openai_compat", "responses", responses, 1000) == 1000
    assert responses["max_output_tokens"] == 1000
    # A server that only knows `max_tokens` would ignore this one: the cap goes in both.
    newer: dict[str, Any] = {"max_completion_tokens": 7}
    assert prepare_body("openai_compat", "chat/completions", newer, 1000) == 7
    assert newer == {"max_completion_tokens": 7, "max_tokens": 7}
    older: dict[str, Any] = {"max_tokens": 10**9}  # OpenAI itself takes only one of the names
    assert prepare_body("openai_compat", "chat/completions", older, 1000, openai) == 1000
    assert older == {"max_completion_tokens": 1000}
    google: dict[str, Any] = {"generationConfig": {"max_output_tokens": 10**9}}
    prepare_body("google", "v1beta/models/x:generateContent", google, 1000)
    assert google["generationConfig"] == {"maxOutputTokens": 1000}


async def test_many_completions_never_reach_the_server_keys(world: dict[str, Any]) -> None:  # noqa: F811
    await save_key(world["db"], world["vault"], "", "openai", "sk-server-openai-0001")
    world["upstream"].body = OPENAI_JSON
    held = reservations(world)
    body = {"model": "gpt-5", "messages": [{"role": "user", "content": "hi"}], "n": 128}
    refused = await post(world, "openai/chat/completions", body)
    assert refused.status_code == 400 and "one completion" in refused.json()["error"]["message"]
    gemini = {"contents": [], "generationConfig": {"candidateCount": 8}}
    path = "gemini/v1beta/models/gemini-2.5-flash:generateContent"
    await save_key(world["db"], world["vault"], "", "gemini", "server-gemini-key-0001")
    assert (await post(world, path, gemini)).status_code == 400
    assert world["upstream"].requests == [] and held == []
    del body["n"]
    assert (await post(world, "openai/chat/completions", body)).status_code == 200
    cap = world["gateway"].settings.max_output_tokens
    assert sent(world)["max_completion_tokens"] == cap
    assert held[0] >= cost("openai", "gpt-5", 0, cap)


# Problem 2: server-side tools ------------------------------------------------------------


async def test_anthropic_web_searches_are_reserved_and_charged(world: dict[str, Any]) -> None:  # noqa: F811
    await save_key(world["db"], world["vault"], "", "anthropic", "sk-ant-server-key-0001")
    world["upstream"].body = SEARCH_SSE
    held = reservations(world)
    search = {"type": "web_search_20250305", "name": "web_search"}
    response = await call(world, token(world, "c2"), tools=[search])
    assert response.status_code == 200
    max_uses = sent(world)["tools"][0]["max_uses"]
    assert 1 <= max_uses <= 10
    assert held[0] >= max_uses * WEB_SEARCH_USD
    rows = await usage_rows(world)
    tokens = cost("anthropic", "claude-sonnet-5-5", 100, 50)
    assert rows[0].cost_usd == pytest.approx(tokens + 3 * WEB_SEARCH_USD)


async def test_a_broken_off_search_is_charged_its_most(world: dict[str, Any]) -> None:  # noqa: F811
    await save_key(world["db"], world["vault"], "", "anthropic", "sk-ant-server-key-0001")
    world["upstream"].body = SEARCH_SSE[: SEARCH_SSE.index("event: message_delta")]
    search = {"type": "web_search_20250305", "name": "web_search", "max_uses": 4}
    await call(world, token(world, "c2"), tools=[search])
    rows = await usage_rows(world)
    assert rows[0].estimated and rows[0].cost_usd >= 4 * WEB_SEARCH_USD


def test_web_searches_are_read_from_the_usage() -> None:
    sniffer = UsageSniffer("anthropic")
    sniffer.feed(SEARCH_SSE.encode())
    counted = sniffer.finish()
    assert counted.reported and counted.web_searches == 3
    assert cost("anthropic", "claude-sonnet-5-5", 0, 0, counted) == pytest.approx(0.03)


async def test_server_tools_the_meter_cannot_price_are_refused(world: dict[str, Any]) -> None:  # noqa: F811
    for provider, secret in (("anthropic", "sk-ant-server-0001"), ("openai", "sk-server-0001"),
                             ("gemini", "gemini-server-0001")):  # fmt: skip
        await save_key(world["db"], world["vault"], "", provider, secret)
    admin = token(world, "c2")
    for tool in ({"type": "code_execution_20250825", "name": "code_execution"},
                 {"type": "web_fetch_20250910", "name": "web_fetch"}):  # fmt: skip
        refused = await call(world, admin, tools=[tool])
        assert refused.status_code == 403 and "server-side tool" in refused.text
    assert (await call(world, admin, mcp_servers=[{"url": "https://x"}])).status_code == 403
    for tool in ({"type": "web_search"}, {"type": "code_interpreter"}, {"type": "file_search"}):
        body = {"model": "gpt-5", "input": "hi", "tools": [tool]}
        assert (await post(world, "openai/responses", body)).status_code == 403
    chat = {"model": "gpt-5", "messages": [], "web_search_options": {}}
    assert (await post(world, "openai/chat/completions", chat)).status_code == 403
    path = "gemini/v1beta/models/gemini-2.5-flash:generateContent"
    for tool in ({"googleSearch": {}}, {"google_search": {}}, {"codeExecution": {}}):
        assert (await post(world, path, {"contents": [], "tools": [tool]})).status_code == 403
    assert world["upstream"].requests == []
    # Plain function tools and Anthropic's client-side tools still pass.
    world["upstream"].body = OPENAI_JSON
    function = {"type": "function", "name": "f", "parameters": {}}
    responses = {"model": "gpt-5", "input": "hi", "tools": [function]}
    assert (await post(world, "openai/responses", responses)).status_code == 200
    gemini = {"contents": [], "tools": [{"functionDeclarations": [{"name": "f"}]}]}
    assert (await post(world, path, gemini)).status_code == 200
    world["upstream"].body = SEARCH_SSE
    tools = [{"name": "f", "input_schema": {}}, {"type": "bash_20250124", "name": "bash"}]
    assert (await call(world, admin, tools=tools)).status_code == 200


async def test_own_keys_may_use_server_side_tools(world: dict[str, Any]) -> None:  # noqa: F811
    await save_key(world["db"], world["vault"], "u1", "anthropic", "sk-ant-real-key-123456")
    tool = {"type": "web_fetch_20250910", "name": "web_fetch"}
    assert (await call(world, token(world), tools=[tool])).status_code == 200


# Problem 3: routing to another model and input stored upstream ---------------------------


async def test_routing_fields_are_refused_on_the_server_keys(world: dict[str, Any]) -> None:  # noqa: F811
    await save_key(world["db"], world["vault"], "", "openrouter", "sk-or-server-0001")
    cheap = {"model": "openai/gpt-5-nano", "messages": []}
    for extra in ({"models": ["openai/gpt-5-nano", "anthropic/claude-fable-5-1"]},
                  {"route": "fallback"}, {"provider": {"order": ["x"]}},
                  {"plugins": [{"id": "web"}]}, {"preset": "p"}):  # fmt: skip
        refused = await post(world, "openrouter/chat/completions", {**cheap, **extra})
        assert refused.status_code == 403, extra
    await save_key(world["db"], world["vault"], "", "openai", "sk-server-0001")
    for extra in ({"previous_response_id": "resp_1"}, {"conversation": "conv_1"}):
        body = {"model": "gpt-5", "input": "hi", **extra}
        assert (await post(world, "openai/responses", body)).status_code == 403
    assert world["upstream"].requests == []
    world["upstream"].body = OPENAI_JSON
    assert (await post(world, "openrouter/chat/completions", cheap)).status_code == 200


async def test_own_keys_may_route(world: dict[str, Any]) -> None:  # noqa: F811
    await save_key(world["db"], world["vault"], "u1", "openrouter", "sk-or-own-key-123456")
    world["upstream"].body = OPENAI_JSON
    body = {"model": "openai/gpt-5-nano", "messages": [], "models": ["anthropic/claude-fable-5-1"]}
    assert (await post(world, "openrouter/chat/completions", body, "c1")).status_code == 200
    assert sent(world)["models"] == ["anthropic/claude-fable-5-1"]
