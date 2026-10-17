"""Which cookie carries the session (security review): over HTTPS only the `__Host-` one, so a
plain cookie another host of the same site set cannot sign the browser into another account."""

from types import SimpleNamespace
from typing import Any

from forge_web.auth.sessions import SECURE_SESSION_COOKIE, SESSION_COOKIE, session_token


def server_at(url: str) -> Any:
    return SimpleNamespace(settings=SimpleNamespace(base_url=lambda: url))


def test_over_https_only_the_host_cookie_counts() -> None:
    planted = {SESSION_COOKIE: "the attacker's session"}
    assert session_token(planted, server_at("https://forge.example.com")) is None
    both = {SESSION_COOKIE: "planted", SECURE_SESSION_COOKIE: "mine"}
    assert session_token(both, server_at("https://forge.example.com")) == "mine"
    assert session_token(planted, server_at("http://127.0.0.1:8420")) == "the attacker's session"
