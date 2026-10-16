"""Microsoft Store submissions (W38b): every token is checked by the stand-in
(tests/msstore_standin.py); nothing is submitted without the approval of the commit and an
admin's (or a trusted creator's) go; the package reaches the submission's blob and the
submission is committed; Microsoft's refusals come back with what to do."""

from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select, update

from forge_web.db.hosting_models import GoLiveApproval
from forge_web.db.models import ProjectMember
from forge_web.db.store_models import MsStoreKey
from forge_web.stores import msstore_release
from forge_web.stores.msstore import UPLOADED, EntraApp, StoreClient, StoreError, blob_problem
from msstore_standin import StoreStandIn
from support import LiveServer, Person, person
from test_hosting_deploy import settings_for, short_polls, until
from test_winworker import (
    FakeDotnet,
    finished,
    short_worker_polls,
    started,
    windows_project,
    worker_online,
    worker_token,
)

__all__ = ["short_polls", "short_worker_polls"]
STORE_ID = "9NBLGGH4R315"


@pytest.fixture
def store() -> Iterator[StoreStandIn]:
    with StoreStandIn() as standin:
        yield standin


async def test_every_token_is_checked_by_the_stand_in(store: StoreStandIn) -> None:
    store.add_listing(STORE_ID)
    good = EntraApp.model_validate(store.entra_app(STORE_ID))
    client = StoreClient(store.url, store.url, good)
    assert "access token" in await client.check()
    found = await client.call("GET", client.app_url(STORE_ID))
    assert found["primaryName"] == "Shop"
    await client.close()
    wrong = good.model_copy(update={"client_secret": "not-the-secret"})
    with pytest.raises(StoreError, match="Invalid client secret"):
        await StoreClient(store.url, store.url, wrong).bearer()
    no_token = httpx.get(f"{store.url}/v1.0/my/applications/{STORE_ID}")
    assert no_token.status_code == 401
    stranger = EntraApp.model_validate(store.entra_app("9NOTHERAPP00"))
    with pytest.raises(StoreError) as refused:
        await StoreClient(store.url, store.url, stranger).call(
            "GET", f"{store.url}/v1.0/my/applications/{STORE_ID}")  # fmt: skip
    assert refused.value.status == 403
    assert blob_problem("https://evil.example.com/x", "https://manage.devcenter.microsoft.com")
    assert not blob_problem("https://in1.blob.core.windows.net/x?sig=1",
                            "https://manage.devcenter.microsoft.com")  # fmt: skip


@pytest.fixture
def server(tmp_path: Path, store: StoreStandIn) -> Iterator[LiveServer]:
    settings = settings_for(tmp_path / "data")
    settings.stores.windows_enabled = True
    settings.stores.msstore_api_url = settings.stores.msstore_login_url = store.url
    with LiveServer(settings) as live:
        yield live


@pytest.fixture
async def client(server: LiveServer) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(base_url=server.url, headers=server.headers(), timeout=60) as c:
        yield c


@pytest.fixture(autouse=True)
def follow_at_once(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(msstore_release, "FOLLOW_EVERY_S", 0.0)


async def built(server: LiveServer, web: Any, client: httpx.AsyncClient, tmp_path: Path) -> Any:
    """A project with a built Windows package; its id and the build's."""
    async with worker_online(server.url, await worker_token(client), FakeDotnet(tmp_path / "w")):
        project_id = await windows_project(server, web)
        build = await started(web, project_id)
        done = await until(lambda: finished(web, project_id, build["id"]))
    assert done["status"] == "built", done["error"]
    return project_id, str(build["id"])


async def save_app(web: Any, app: dict[str, str]) -> None:
    saved = await web.request("PUT", "/api/me/msstore-key", json=app)
    assert saved.status_code == 200, saved.text
    assert "client_secret" not in saved.json()


async def submit(web: Any, project_id: str, build_id: str) -> httpx.Response:
    return await web.request("POST", f"/api/projects/{project_id}/windows/submissions",
                             json={"build_id": build_id, "store_id": STORE_ID})  # fmt: skip


async def settled(web: Any, project_id: str, submission_id: str) -> Any:
    rows = (await web.get(f"/api/projects/{project_id}/windows/submissions")).json()
    row = next(r for r in rows if r["id"] == submission_id)
    return row if row["status"] not in ("waiting", "submitting") else None


async def test_nothing_is_submitted_without_the_approval(
    server: LiveServer, client: httpx.AsyncClient, store: StoreStandIn, tmp_path: Path
) -> None:
    store.add_listing(STORE_ID)
    maria: Person = await person(server, "maria")
    project_id, build_id = await built(server, client, client, tmp_path)
    async with server.services.db.session() as session, session.begin():
        session.add(ProjectMember(project_id=project_id, user_id=maria.id, role="editor"))
    assert "Entra ID" in (await submit(maria.web, project_id, build_id)).text
    await save_app(maria.web, store.entra_app(STORE_ID))
    async with server.services.db.session() as session, session.begin():
        commit = (await session.scalars(select(GoLiveApproval.commit))).all()
        await session.execute(update(GoLiveApproval).values(commit="0" * 40))
    unapproved = await submit(maria.web, project_id, build_id)
    assert unapproved.status_code == 409 and "approved" in unapproved.text
    async with server.services.db.session() as session, session.begin():
        await session.execute(update(GoLiveApproval).values(commit=commit[-1]))
    waiting = (await submit(maria.web, project_id, build_id)).json()
    assert waiting["status"] == "waiting" and store.state.blobs == {}  # nothing went out
    approve = f"/api/admin/stores/msstore/{waiting['id']}/approve"
    assert (await maria.web.post(approve)).status_code == 403  # not an admin
    assert (await client.post(approve)).status_code == 200
    done = await until(lambda: settled(maria.web, project_id, waiting["id"]))
    assert (done["status"], done["store_status"]) == ("submitted", "PreProcessing"), done
    sent = store.state.submissions[done["submission_id"]]
    new = [p for p in sent["applicationPackages"] if p["fileStatus"] == "PendingUpload"]
    assert [p["fileName"] for p in new] == [UPLOADED]
    assert store.state.blobs[done["submission_id"]][:2] == b"PK"  # a ZIP with the package
    async with server.services.db.session() as session:
        kept = (await session.scalars(select(MsStoreKey))).one()
    assert "client_secret" not in kept.secret  # sealed by the vault


async def test_refusals_say_what_to_do(
    server: LiveServer, client: httpx.AsyncClient, store: StoreStandIn, tmp_path: Path
) -> None:
    listing = store.add_listing(STORE_ID)
    project_id, build_id = await built(server, client, client, tmp_path)
    await save_app(client, store.entra_app("9NOTHERAPP00"))  # it manages another listing
    first = (await submit(client, project_id, build_id)).json()
    assert first["admin"] == "admin"  # the dev user is an admin: no second approval
    forbidden = await until(lambda: settled(client, project_id, first["id"]))
    assert forbidden["status"] == "failed" and "Manager" in forbidden["hint"]
    await save_app(client, store.entra_app(STORE_ID))
    listing.pending = "1234"  # someone opened a submission in Partner Center
    second = (await submit(client, project_id, build_id)).json()
    open_one = await until(lambda: settled(client, project_id, second["id"]))
    assert "in progress" in open_one["error"] and "Partner Center" in open_one["hint"]
    listing.pending = ""
    third = (await submit(client, project_id, build_id)).json()
    done = await until(lambda: settled(client, project_id, third["id"]))
    assert done["status"] == "submitted", done
    sent = store.state.submissions[done["submission_id"]]
    sent["status"], sent["statusDetails"] = (
        "CertificationFailed",
        {"errors": [{"code": "Policy", "details": "10.1.4.4 the app crashed on start"}]},
    )
    failed = await until(lambda: failed_now(client, project_id, third["id"]))
    assert "crashed on start" in failed["error"]


async def failed_now(client: httpx.AsyncClient, project_id: str, submission_id: str) -> Any:
    row = await settled(client, project_id, submission_id)
    return row if row and row["status"] == "failed" else None
