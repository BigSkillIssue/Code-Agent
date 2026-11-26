"""SEC-W1: nobody becomes a member, an owner, the owner of record or the payer of a project
without accepting. Adding someone (or making a member an owner) only invites them."""

import secrets
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import select

from forge_web.db.models import ApiKey, AuditEntry, Chat, Project, ProjectMember, User
from forge_web.gateway.keys import choose_key
from forge_web.settings import load_settings
from support import LiveServer, Person, person


@dataclass
class Shop:
    """An attacker's project (owner + its chat) and a victim with their own Anthropic key."""

    server: LiveServer
    attacker: Person
    victim: Person
    project_id: str
    chat_id: str

    @property
    def members(self) -> str:
        return f"/api/projects/{self.project_id}/members"


@pytest.fixture
async def shop(tmp_path: Path) -> AsyncIterator[Shop]:
    data_dir = tmp_path / "data"
    settings = load_settings(
        data_dir / "forge-web.toml",
        environ={"FORGE_WEB_DATA_DIR": str(data_dir)},
        overrides={"sandbox.isolation": "docker", "sandbox.docker": "forge-web-test-no-docker"},
    )
    with LiveServer(settings) as server:
        attacker, victim = await person(server, "attacker"), await person(server, "victim")
        services = server.services
        now, pid, cid = time.time(), secrets.token_hex(8), secrets.token_hex(16)
        async with services.db.session() as s, s.begin():
            s.add(ApiKey(id=secrets.token_hex(8), owner_id=victim.id, provider="anthropic",
                         secret=services.vault.encrypt("sk-ant-VICTIM"), name="",
                         created_at=now))  # fmt: skip
            s.add(Project(id=pid, name="Mine", owner_id=attacker.id, created_at=now,
                          updated_at=now))  # fmt: skip
            await s.flush()
            s.add(ProjectMember(project_id=pid, user_id=attacker.id, role="owner"))
            s.add(Chat(id=cid, project_id=pid, user_id=attacker.id, shared=True,
                       created_at=now, updated_at=now))  # fmt: skip
        try:
            yield Shop(server, attacker, victim, pid, cid)
        finally:
            for who in (attacker, victim):
                await who.web.client.aclose()


async def payer_and_key(shop: Shop) -> tuple[str, str, str | None]:
    """The project's owner of record, its chat's payer and the key the gateway would use."""
    services = shop.server.services
    async with services.db.session() as s:
        project, chat = await s.get(Project, shop.project_id), await s.get(Chat, shop.chat_id)
        assert project is not None and chat is not None
        payer = await s.get(User, chat.user_id)
    assert payer is not None
    key = await choose_key(services.db, services.vault, payer, "anthropic", False)
    return project.owner_id, chat.user_id, key.secret if key is not None else None


async def role_of(shop: Shop, user_id: str) -> str | None:
    async with shop.server.services.db.session() as s:
        member = await s.get(ProjectMember, (shop.project_id, user_id))
    return member.role if member is not None else None


async def test_an_added_owner_is_only_invited_and_pays_nothing(shop: Shop) -> None:
    attacker, victim = shop.attacker.web, shop.victim
    added = await attacker.post(shop.members, {"email": victim.email, "role": "owner"})
    assert added.status_code == 201 and added.json()["pending"] is True
    assert await role_of(shop, victim.id) is None  # not a member before accepting
    assert (await victim.web.get(f"/api/projects/{shop.project_id}")).status_code == 404
    step_down = await attacker.request("PATCH", f"{shop.members}/{shop.attacker.id}",
                                       json={"role": "editor"})  # fmt: skip
    assert step_down.status_code == 409 and "accepted" in step_down.json()["detail"]
    leave = await attacker.request("DELETE", f"{shop.members}/{shop.attacker.id}")
    assert leave.status_code == 409
    owner, payer, key = await payer_and_key(shop)
    assert owner == payer == shop.attacker.id and key != "sk-ant-VICTIM"

    invites = (await victim.web.get("/api/invitations")).json()
    assert [(i["project_id"], i["role"], i["invited_by"]["email"]) for i in invites] == [
        (shop.project_id, "owner", shop.attacker.email)
    ]
    declined = await victim.web.request("DELETE", f"/api/invitations/{shop.project_id}")
    assert declined.status_code == 204
    assert (await victim.web.get("/api/invitations")).json() == []
    again = await victim.web.post(f"/api/invitations/{shop.project_id}")
    assert again.status_code == 404  # a declined invitation cannot be accepted any more
    emails = [m["email"] for m in (await attacker.get(shop.members)).json()]
    assert emails == [shop.attacker.email]


async def test_promoting_a_member_to_owner_waits_for_their_consent(shop: Shop) -> None:
    attacker, victim = shop.attacker.web, shop.victim
    await attacker.post(shop.members, {"email": victim.email, "role": "viewer"})
    accepted = await victim.web.post(f"/api/invitations/{shop.project_id}")
    assert accepted.status_code == 200 and accepted.json()["role"] == "viewer"
    assert await role_of(shop, victim.id) == "viewer"
    promote = await attacker.request("PATCH", f"{shop.members}/{victim.id}",
                                     json={"role": "owner"})  # fmt: skip
    assert promote.status_code == 202 and promote.json() == {
        "user_id": victim.id,
        "role": "viewer",
        "pending": "owner",
    }
    assert await role_of(shop, victim.id) == "viewer"
    step_down = await attacker.request("PATCH", f"{shop.members}/{shop.attacker.id}",
                                       json={"role": "editor"})  # fmt: skip
    assert step_down.status_code == 409
    assert (await payer_and_key(shop))[:2] == (shop.attacker.id, shop.attacker.id)

    # Once the victim agrees, they are an owner and may become the owner of record.
    upgraded = await victim.web.post(f"/api/invitations/{shop.project_id}")
    assert upgraded.status_code == 200 and upgraded.json()["role"] == "owner"
    step_down = await attacker.request("PATCH", f"{shop.members}/{shop.attacker.id}",
                                       json={"role": "editor"})  # fmt: skip
    assert step_down.status_code == 200
    assert await payer_and_key(shop) == (victim.id, victim.id, "sk-ant-VICTIM")
    async with shop.server.services.db.session() as s:
        actions = set(await s.scalars(select(AuditEntry.action)))
    assert {"member_invited", "member_added", "member_role"} <= actions


async def test_invitations_belong_to_the_invitee_and_owners_withdraw_them(shop: Shop) -> None:
    attacker, victim = shop.attacker.web, shop.victim
    other = await person(shop.server, "other")
    await attacker.post(shop.members, {"email": victim.email, "role": "editor"})
    twice = await attacker.post(shop.members, {"email": victim.email, "role": "owner"})
    assert twice.status_code == 409  # one open invitation per person
    stolen = await other.web.post(f"/api/invitations/{shop.project_id}")
    assert stolen.status_code == 404 and await role_of(shop, other.id) is None
    pending = await attacker.request("PATCH", f"{shop.members}/{victim.id}",
                                     json={"role": "owner"})  # fmt: skip
    assert pending.status_code == 404  # not a member yet
    listed = (await attacker.get(shop.members)).json()
    assert {(m["email"], m["role"], m["pending"]) for m in listed} == {
        (shop.attacker.email, "owner", False), (victim.email, "editor", True)
    }  # fmt: skip
    withdrawn = await attacker.request("DELETE", f"{shop.members}/{victim.id}")
    assert withdrawn.status_code == 204
    assert (await victim.web.get("/api/invitations")).json() == []
    assert (await victim.web.post(f"/api/invitations/{shop.project_id}")).status_code == 404
    await other.web.client.aclose()
