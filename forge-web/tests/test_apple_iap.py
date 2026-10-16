"""Apple in-app purchases on the server (W36): a notification counts only when Apple's chain
signed it; a renewal extends what the customer bought and a refund takes it away; a repeated
notification counts once; an app reads only its own customers' purchases. Apple is the stand-in
signer (tests/asc_standin_iap.py), trusted here in place of Apple's root."""

import json
import secrets
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from asc_standin_iap import AppleSigner, subscription
from forge_web.apple import iap
from forge_web.billing.relay_tokens import payments_token
from forge_web.db.billing_models import AppleNotification
from forge_web.db.hosting_models import HostedApp
from forge_web.db.models import AppleRelease, Project
from forge_web.hosting.hosts import create_host
from host_standin import HostStandIn
from support import LiveServer, dev_settings

BUNDLE = "com.example.shop"
PRO = "com.example.shop.pro"


@pytest.fixture
def apple(monkeypatch: pytest.MonkeyPatch) -> AppleSigner:
    signer = AppleSigner()
    monkeypatch.setattr(iap, "TRUSTED_ROOTS", [signer.root])
    return signer


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    with LiveServer(dev_settings(tmp_path / "data")) as live:
        yield live


@pytest.fixture
async def apple_http(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    """Apple's servers, and the hosted apps: no session, no cookies."""
    async with httpx.AsyncClient(base_url=server.url, timeout=30) as client:
        yield client


async def hosted_app(server: LiveServer, app: str, bundle: str = BUNDLE) -> tuple[str, str]:
    """A hosted app with an Apple release of `bundle`; its project and staging relay token."""
    db, now, project_id = server.services.db, time.time(), secrets.token_hex(8)
    host, _ = await create_host(db, f"host-{app}", HostStandIn().public_key)
    owner = server.services.dev_user_id
    async with db.session() as session, session.begin():
        session.add(Project(id=project_id, name=app, owner_id=owner, source="app",
                            created_at=now, updated_at=now))  # fmt: skip
        await session.flush()
        session.add(HostedApp(project_id=project_id, app=app, host_id=host.id, created_at=now))
        session.add(AppleRelease(id=secrets.token_hex(8), project_id=project_id, user_id=owner,
                                 approval_id="a1", commit="c" * 40, platform="ios",
                                 build_number=1, step="done", status="done",
                                 data=json.dumps({"bundle_id": bundle}), created_at=now,
                                 updated_at=now))  # fmt: skip
    return project_id, await payments_token(db, server.services.vault, project_id, "staging")


async def tell(client: httpx.AsyncClient, app: str, body: dict[str, str]) -> httpx.Response:
    return await client.post(f"/api/apple/notifications/{app}", json=body)


async def bought(client: httpx.AsyncClient, token: str, **query: str) -> list[dict[str, Any]]:
    answer = await client.get("/api/relay/payments/apple", params=query,
                              headers={"Authorization": f"Bearer {token}"})  # fmt: skip
    assert answer.status_code == 200, answer.text
    return list(answer.json())


async def test_a_valid_chain_is_accepted_and_a_broken_or_foreign_one_refused(
    server: LiveServer, apple_http: httpx.AsyncClient, apple: AppleSigner
) -> None:
    _, token = await hosted_app(server, "shop")
    purchase = subscription("1000", PRO, 30, appAccountToken="u-7")
    assert (await tell(apple_http, "shop", apple.notification("SUBSCRIBED", BUNDLE, purchase))
            ).json() == {"ok": True, "repeated": False}  # fmt: skip
    assert [p["product_id"] for p in await bought(apple_http, token)] == [PRO]
    stranger = AppleSigner()  # a chain of its own, not ending in the trusted root
    foreign = stranger.notification("SUBSCRIBED", BUNDLE, subscription("2000", PRO, 30))
    assert (await tell(apple_http, "shop", foreign)).status_code == 400
    swapped = apple.notification("SUBSCRIBED", BUNDLE, subscription("3000", PRO, 30))
    head, body, _ = swapped["signedPayload"].split(".")
    other_sig = stranger.sign({"x": 1}).split(".")[2]  # a signature by another key
    broken = {"signedPayload": f"{head}.{body}.{other_sig}"}
    assert (await tell(apple_http, "shop", broken)).status_code == 400
    mixed = [apple.leaf, stranger.intermediate, apple.root]  # the leaf's issuer is not this one
    unchained = {"signedPayload": apple.sign({"notificationUUID": "x"}, chain=mixed)}
    assert (await tell(apple_http, "shop", unchained)).status_code == 400
    unmarked = AppleSigner(marks=False)
    iap.TRUSTED_ROOTS.append(unmarked.root)  # trusted, but without Apple's marks
    plain = unmarked.notification("SUBSCRIBED", BUNDLE, subscription("4000", PRO, 30))
    assert (await tell(apple_http, "shop", plain)).status_code == 400
    elsewhere = apple.notification("SUBSCRIBED", "com.other.app", subscription("5000", PRO, 30))
    assert (await tell(apple_http, "shop", elsewhere)).status_code == 403
    assert (await tell(apple_http, "shop", {"signedPayload": "a.b"})).status_code == 400
    assert len(await bought(apple_http, token)) == 1


async def test_a_renewal_extends_and_a_refund_removes_the_entitlement(
    server: LiveServer, apple_http: httpx.AsyncClient, apple: AppleSigner
) -> None:
    _, token = await hosted_app(server, "shop")
    first = subscription("1000", PRO, 30, appAccountToken="u-7")
    await tell(apple_http, "shop", apple.notification("SUBSCRIBED", BUNDLE, first))
    [mine] = await bought(apple_http, token, account_token="u-7")
    renewal = subscription("1000", PRO, 60, appAccountToken="u-7")
    await tell(apple_http, "shop", apple.notification("DID_RENEW", BUNDLE, renewal))
    [renewed] = await bought(apple_http, token, account_token="u-7")
    assert renewed["expires_at"] > mine["expires_at"] + 25 * 86400  # thirty days more
    assert await bought(apple_http, token, account_token="someone-else") == []
    refund = subscription("1000", PRO, 60, revocationDate=int(time.time() * 1000))
    await tell(apple_http, "shop", apple.notification("REFUND", BUNDLE, refund))
    assert await bought(apple_http, token) == []
    lapsed = subscription("9000", PRO, -1)  # its period is over
    await tell(apple_http, "shop", apple.notification("EXPIRED", BUNDLE, lapsed))
    assert await bought(apple_http, token) == []
    production = apple.notification("SUBSCRIBED", BUNDLE, subscription("7000", PRO, 30),
                                    environment="Production")  # fmt: skip
    await tell(apple_http, "shop", production)
    assert await bought(apple_http, token) == []  # staging's token sees sandbox purchases only


async def test_a_repeated_notification_counts_once(
    server: LiveServer, apple_http: httpx.AsyncClient, apple: AppleSigner
) -> None:
    project_id, token = await hosted_app(server, "shop")
    await tell(apple_http, "shop", apple.notification("SUBSCRIBED", BUNDLE,
                                                      subscription("1000", PRO, 30)))  # fmt: skip
    refund = apple.notification("REFUND", BUNDLE, subscription("1000", PRO, 30), "n-1")
    reversal = apple.notification("REFUND_REVERSED", BUNDLE, subscription("1000", PRO, 30))
    assert (await tell(apple_http, "shop", refund)).json()["repeated"] is False
    await tell(apple_http, "shop", reversal)
    assert len(await bought(apple_http, token)) == 1
    again = await tell(apple_http, "shop", refund)  # Apple sends it again: it changes nothing
    assert again.json() == {"ok": True, "repeated": True}
    assert len(await bought(apple_http, token)) == 1
    async with server.services.db.session() as session:
        count = await session.scalar(select(func.count()).select_from(AppleNotification).where(
            AppleNotification.project_id == project_id))  # fmt: skip
    assert count == 3


async def test_an_app_reads_only_its_own_customers(
    server: LiveServer, apple_http: httpx.AsyncClient, apple: AppleSigner
) -> None:
    _, shop = await hosted_app(server, "shop")
    blog_project, blog = await hosted_app(server, "blog", "com.example.blog")
    await tell(apple_http, "shop", apple.notification("SUBSCRIBED", BUNDLE,
                                                      subscription("1000", PRO, 30)))  # fmt: skip
    assert len(await bought(apple_http, shop)) == 1 and await bought(apple_http, blog) == []
    sent_elsewhere = apple.notification("SUBSCRIBED", BUNDLE, subscription("2000", PRO, 30))
    assert (await tell(apple_http, "blog", sent_elsewhere)).status_code == 403
    assert (await tell(apple_http, "nobody", sent_elsewhere)).status_code == 404
    no_token = await apple_http.get("/api/relay/payments/apple")
    assert no_token.status_code == 401
    async with server.services.db.session() as session, session.begin():
        row = await session.get(HostedApp, blog_project)
        assert row is not None
        row.suspended = True
    stopped = await apple_http.get("/api/relay/payments/apple",
                                   headers={"Authorization": f"Bearer {blog}"})  # fmt: skip
    assert stopped.status_code == 403
