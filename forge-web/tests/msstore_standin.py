"""A stand-in for the Microsoft Store (W38b): Entra ID's token endpoint, the parts of the
Partner Center submission API Forge Web uses, and the submission's blob storage, with
Microsoft's checks where they matter.

A token needs a client secret Entra ID knows, for the Partner Center resource; every API call
needs a token it gave out, and the Entra ID app must manage the Store listing. An app has at
most one open submission. The blob takes only its own signed URL. A commit needs every package
the submission lists inside the uploaded ZIP.

Tests never call Microsoft. Start it with `with StoreStandIn() as store:` and point
`[stores] msstore_api_url` and `msstore_login_url` at `store.url`.
"""

import io
import secrets
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

RESOURCE = "https://manage.devcenter.microsoft.com"
APP = "/v1.0/my/applications/{store_id}"


@dataclass
class Listing:
    """An app as Partner Center knows it."""

    name: str
    pending: str = ""  # the open submission
    published: list[dict[str, Any]] = field(default_factory=list)  # its packages


@dataclass
class StoreState:
    """What Microsoft knows and what it was sent."""

    secrets: dict[tuple[str, str], str] = field(default_factory=dict)  # (tenant, client) -> secret
    access: dict[str, set[str]] = field(default_factory=dict)  # client -> Store IDs it manages
    listings: dict[str, Listing] = field(default_factory=dict)
    tokens: dict[str, tuple[str, float]] = field(default_factory=dict)  # token -> client, until
    submissions: dict[str, dict[str, Any]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)  # submission -> the uploaded ZIP
    signatures: dict[str, str] = field(default_factory=dict)  # submission -> its blob's sig
    calls: list[tuple[str, str]] = field(default_factory=list)
    base_url: str = ""


def failure(status: int, code: str, message: str) -> JSONResponse:
    """An API error the way Partner Center writes it."""
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def missing_packages(state: StoreState, submission: dict[str, Any]) -> list[str]:
    """The packages a submission lists for upload that its ZIP does not hold."""
    wanted = [p["fileName"] for p in submission.get("applicationPackages", [])
              if p.get("fileStatus") == "PendingUpload"]  # fmt: skip
    try:
        with zipfile.ZipFile(io.BytesIO(state.blobs.get(submission["id"], b""))) as archive:
            names = set(archive.namelist())
    except zipfile.BadZipFile:
        names = set()
    return [name for name in wanted if name not in names] if wanted else ["(no new package)"]


def standin_app(state: StoreState) -> FastAPI:
    """Entra ID, the submission API and the blob."""
    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request: Request, call_next: Any) -> Response:
        path = request.url.path
        state.calls.append((request.method, path))
        if not path.startswith("/v1.0/my/"):
            return await call_next(request)  # type: ignore[no-any-return]
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        client, until = state.tokens.get(token, ("", 0.0))
        if not client or until < time.time():
            return failure(401, "Unauthorized", "The access token is missing or invalid.")
        store_id = path.removeprefix("/v1.0/my/applications/").split("/", 1)[0]
        if store_id not in state.listings:
            return failure(404, "NotFound", "The application was not found.")
        if store_id not in state.access.get(client, set()):
            return failure(403, "Forbidden", "Forbidden")
        return await call_next(request)  # type: ignore[no-any-return]

    @app.post("/{tenant}/oauth2/token")
    async def token(tenant: str, request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        known = state.secrets.get((tenant, form.get("client_id", "")))
        if form.get("grant_type") != "client_credentials" or form.get("resource") != RESOURCE:
            return JSONResponse({"error": "invalid_request",
                                 "error_description": "AADSTS900144: bad grant"}, 400)  # fmt: skip
        if known is None or not secrets.compare_digest(known, form.get("client_secret", "")):
            refusal = "AADSTS7000215: Invalid client secret provided."
            return JSONResponse({"error": "invalid_client", "error_description": refusal}, 401)
        made = secrets.token_urlsafe(24)
        state.tokens[made] = (form["client_id"], time.time() + 3600)
        return JSONResponse({"token_type": "Bearer", "expires_in": "3599", "access_token": made})

    submission_routes(app, state)
    return app


def submission_routes(app: FastAPI, state: StoreState) -> None:
    """The app, its submissions, their blob, commit and status."""

    @app.get(APP)
    async def application(store_id: str) -> Any:
        listing = state.listings[store_id]
        found: dict[str, Any] = {"id": store_id, "primaryName": listing.name}
        if listing.pending:
            found["pendingApplicationSubmission"] = {"id": listing.pending}
        return found

    @app.post(APP + "/submissions")
    async def new_submission(store_id: str) -> Any:
        listing = state.listings[store_id]
        if listing.pending:
            return failure(409, "InvalidState", "An in-progress submission exists.")
        made = str(uuid.uuid4().int)[:19]
        state.signatures[made] = secrets.token_urlsafe(16)
        listing.pending = made
        state.submissions[made] = {
            "id": made, "status": "PendingCommit", "statusDetails": {"errors": []},
            "applicationPackages": [dict(p) for p in listing.published],
            "fileUploadUrl": f"{state.base_url}/blob/{made}?sv=2026&sig={state.signatures[made]}",
        }  # fmt: skip
        return state.submissions[made]

    @app.put(APP + "/submissions/{submission_id}")
    async def change(store_id: str, submission_id: str, request: Request) -> Any:
        if submission_id not in state.submissions:
            return failure(404, "NotFound", "The submission was not found.")
        body = await request.json()
        state.submissions[submission_id]["applicationPackages"] = body["applicationPackages"]
        return state.submissions[submission_id]

    @app.put("/blob/{submission_id}")
    async def blob(submission_id: str, request: Request) -> Response:
        if request.query_params.get("sig") != state.signatures.get(submission_id):
            return Response(status_code=403)
        if request.headers.get("x-ms-blob-type") != "BlockBlob":
            return Response(status_code=400)
        state.blobs[submission_id] = await request.body()
        return Response(status_code=201)

    @app.post(APP + "/submissions/{submission_id}/commit")
    async def commit(store_id: str, submission_id: str) -> Any:
        submission = state.submissions[submission_id]
        missing = missing_packages(state, submission)
        if missing:
            submission["status"] = "CommitFailed"
            submission["statusDetails"] = {
                "errors": [
                    {"code": "InvalidParameterValue", "details": f"missing: {', '.join(missing)}"}
                ]
            }
        else:
            submission["status"] = "PreProcessing"
        return {"status": "CommitStarted"}

    @app.get(APP + "/submissions/{submission_id}/status")
    async def status(store_id: str, submission_id: str) -> Any:
        submission = state.submissions[submission_id]
        return {"status": submission["status"], "statusDetails": submission["statusDetails"]}


class StoreStandIn:
    """The stand-in on a free local port, in a background thread."""

    def __init__(self) -> None:
        import uvicorn

        self.state = StoreState()
        config = uvicorn.Config(standin_app(self.state), host="127.0.0.1", port=0,
                                log_level="warning", loop="asyncio")  # fmt: skip
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "StoreStandIn":
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("the Microsoft Store stand-in did not start")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        self.state.base_url = self.url
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(30)

    def entra_app(self, *store_ids: str) -> dict[str, str]:
        """An Entra ID app that manages these listings: what the maker gives Forge Web."""
        tenant, client, secret = str(uuid.uuid4()), str(uuid.uuid4()), secrets.token_urlsafe(24)
        self.state.secrets[(tenant, client)] = secret
        self.state.access[client] = set(store_ids)
        return {"tenant_id": tenant, "client_id": client, "client_secret": secret}

    def add_listing(self, store_id: str, name: str = "Shop") -> Listing:
        """An app as its maker reserves it once in Partner Center."""
        self.state.listings[store_id] = Listing(name=name)
        return self.state.listings[store_id]

    def publish(self, submission_id: str) -> None:
        """Microsoft certified and published a submission: it is no longer open."""
        submission = self.state.submissions[submission_id]
        submission["status"] = "Published"
        for listing in self.state.listings.values():
            if listing.pending == submission_id:
                listing.pending = ""
                listing.published = [dict(p, fileStatus="Uploaded")
                                     for p in submission["applicationPackages"]
                                     if p.get("fileStatus") != "PendingDelete"]  # fmt: skip
