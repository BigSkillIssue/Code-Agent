"""Approving an Apple app (W21): the guideline reviews and the open approval question of a
project in one place, and an approval that only the user's own click records."""

import asyncio
import json
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge.apple_flow import APPROVE, NOT_YET, SEND_BACK
from sqlalchemy import select

from forge_web.apple.questions import is_approval
from forge_web.db.models import AppleApproval, AppleJob, AuditEntry, ChatEvent
from support import Browser, LiveServer, call, dev_settings, fake_script

APPROVAL = {"text": "Is the app ready for Apple? The Apple review: fine.", "kind": "choice",
            "options": [NOT_YET, APPROVE, SEND_BACK], "default": NOT_YET,
            "why": "Only an app you approve counts as ready for the App Store."}  # fmt: skip


@pytest.fixture
def server(tmp_path: Path) -> Iterator[LiveServer]:
    script = fake_script(call("ask_user", questions=[APPROVAL]), {"text": "Thanks."})
    with LiveServer(dev_settings(tmp_path / "data", script)) as live:
        yield live


def client_of(server: LiveServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60)


async def asked_chat(
    server: LiveServer, client: httpx.AsyncClient
) -> tuple[str, str, dict[str, Any]]:
    """An Apple project whose chat waits on the approval question."""
    project = (await client.post("/api/projects", json={"name": "Tally", "source": "apple"})).json()
    chat = (await client.post(f"/api/projects/{project['id']}/chats", json={})).json()
    async with Browser(server) as tab:
        await tab.send({"type": "subscribe", "chat_id": chat["id"], "after_seq": 0})
        await tab.next("subscribed")
        await client.post(f"/api/chats/{chat['id']}/messages", json={"text": "Ship it"})
        request = (await tab.next_item("request"))["item"]
    return project["id"], chat["id"], request


async def review_of(client: httpx.AsyncClient, project_id: str, pending: bool) -> dict[str, Any]:
    """The review page's data, once its approval question is (no longer) pending."""
    async with asyncio.timeout(20):
        while True:
            found = (await client.get(f"/api/projects/{project_id}/apple/review")).json()
            if (found["pending"] is not None) == pending:
                return found
            await asyncio.sleep(0.1)


def answer(request_id: str, choice: str) -> dict[str, Any]:
    return {"request_id": request_id, "answer": {"answers": [{"question_index": 0,
                                                              "values": [choice]}]}}  # fmt: skip


async def audits(server: LiveServer, prefix: str) -> list[AuditEntry]:
    async with server.services.db.session() as session:
        rows = await session.scalars(select(AuditEntry).where(AuditEntry.action.like(f"{prefix}%")))
        return list(rows)


def test_only_forges_approval_question_counts() -> None:
    assert is_approval({"questions": [APPROVAL]})
    assert not is_approval({"questions": [APPROVAL, APPROVAL]})
    assert not is_approval({"questions": [{**APPROVAL, "options": [APPROVE, NOT_YET]}]})
    assert not is_approval({"questions": [{**APPROVAL, "kind": "multi"}]})
    assert not is_approval({"call": {}}) and not is_approval(None)


async def test_approving_records_who_approved_which_commit(server: LiveServer) -> None:
    async with client_of(server) as client:
        project_id, chat_id, request = await asked_chat(server, client)
        assert request["purpose"] == "apple_approval"  # the web UI shows it as the approval
        waiting = await review_of(client, project_id, pending=True)
        assert waiting["pending"]["request_id"] == request["id"]
        assert waiting["pending"]["chat_id"] == chat_id
        assert waiting["choices"] == {
            "approve": APPROVE,
            "send_back": SEND_BACK,
            "not_yet": NOT_YET,
        }
        assert waiting["approvals"] == []
        done = await client.post(
            f"/api/chats/{chat_id}/answer", json=answer(request["id"], APPROVE)
        )
        assert done.json() == {"accepted": True}
        approved = await review_of(client, project_id, pending=False)
        log = (await client.get(f"/api/projects/{project_id}/git/log")).json()["commits"]
    [entry] = approved["approvals"]
    assert entry["commit"] == log[0]["commit"] and entry["clean"] is True
    assert log[-1]["subject"] == "Start from Forge's Apple app template"  # nothing uncommitted
    assert entry["chat_id"] == chat_id
    assert entry["user"] and entry["at"] > 0
    [audit] = await audits(server, "apple.")
    assert audit.action == "apple.approved" and audit.target == project_id


async def test_sending_it_back_approves_nothing(server: LiveServer) -> None:
    async with client_of(server) as client:
        project_id, chat_id, request = await asked_chat(server, client)
        body = answer(request["id"], SEND_BACK)
        assert (await client.post(f"/api/chats/{chat_id}/answer", json=body)).json()["accepted"]
        after = await review_of(client, project_id, pending=False)
        late = await client.post(
            f"/api/chats/{chat_id}/answer", json=answer(request["id"], APPROVE)
        )
        assert late.json() == {"accepted": False}  # already answered: no approval sneaks in
    assert after["approvals"] == []
    assert [a.action for a in await audits(server, "apple.")] == ["apple.sent_back"]
    async with server.services.db.session() as session:
        assert list(await session.scalars(select(AppleApproval))) == []


async def test_the_page_shows_the_latest_review_of_each_stage_and_the_builds(
    server: LiveServer,
) -> None:
    async with client_of(server) as client:
        project = (
            await client.post("/api/projects", json={"name": "Tally", "source": "apple"})
        ).json()
        chat = (await client.post(f"/api/projects/{project['id']}/chats", json={})).json()
        other = (await client.post("/api/projects", json={"name": "Other"})).json()
        other_chat = (await client.post(f"/api/projects/{other['id']}/chats", json={})).json()
        user_id = (await client.get("/api/me")).json()["id"]
        now = time.time()
        mine, theirs = chat["id"], other_chat["id"]
        reviews = [(mine, 1, "prompt", "concern", "old"), (mine, 2, "prompt", "ok", "new"),
                   (mine, 3, "product", "violation", "the app"),
                   (theirs, 1, "plan", "ok", "someone else's")]  # fmt: skip
        async with server.services.db.session() as session, session.begin():
            for chat_id, seq, stage, verdict, summary in reviews:
                event = {"kind": "guideline_review", "session_id": "s", "agent_id": "reviewer",
                         "ts": now + seq, "stage": stage, "verdict": verdict, "summary": summary,
                         "findings": [{"area": "design", "status": verdict, "guideline": "4.2",
                                       "reason": "thin", "fix": "add features"}]}  # fmt: skip
                data = json.dumps({"type": "event", "event": event})
                session.add(ChatEvent(chat_id=chat_id, seq=seq, ts=now + seq, type="event",
                                      kind="guideline_review", data=data))  # fmt: skip
            session.add(AppleJob(id="j1", project_id=project["id"], chat_id=chat["id"],
                                 user_id=user_id, kind="build", status="done", created_at=now,
                                 params=json.dumps({"platform": "ios", "action": "test"}),
                                 seconds=42, outcome="test on ios: succeeded"))  # fmt: skip
        found = (await client.get(f"/api/projects/{project['id']}/apple/review")).json()
    shown = [(r["stage"], r["summary"]) for r in found["reviews"]]
    assert shown == [("prompt", "new"), ("product", "the app")]  # newest per stage, in order
    assert found["reviews"][1]["findings"][0]["guideline"] == "4.2"
    assert found["reviews"][0]["chat_id"] == chat["id"]
    assert [(j["kind"], j["outcome"]) for j in found["builds"]] == [
        ("build", "test on ios: succeeded")
    ]
    assert found["builds"][0]["params"] == {"platform": "ios", "action": "test"}
    assert found["pending"] is None
