"""A stand-in for Apple's App Store Connect API (W22): the parts Forge Web uses, with Apple's
checks where they matter. Every request must carry a valid ES256 JWT of the test key.

Tests never call Apple. Start it with `with AscStandIn() as asc:` and point
`[apple] asc_api_url` at `asc.url`; `asc.state` holds what it was sent.
"""

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from asc_standin_release import (
    ReleaseState,
    new_id,
    provisioning_routes,
    resource,
    testflight_routes,
    upload_routes,
)
from asc_standin_store import StoreState, library_routes

KEY_ID, ISSUER_ID, TEAM_ID = "ABC123DEFG", "69a6de7e-1111-47e3-e053-5b8c7c11a4d1", "TEAM123456"


def new_key() -> tuple[str, ec.EllipticCurvePublicKey]:
    """A fresh P-256 key: the PEM a user would upload, and the public half Apple keeps."""
    private = ec.generate_private_key(ec.SECP256R1())
    pem = private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()  # fmt: skip
    return pem, private.public_key()


@dataclass
class AscState:
    """What App Store Connect knows and what it was asked."""

    public_key: ec.EllipticCurvePublicKey
    apps: list[dict[str, Any]] = field(default_factory=list)
    bundle_ids: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    refuse_identifiers: bool = False  # an individual key: no access to identifiers
    release: ReleaseState = field(default_factory=ReleaseState)
    store: StoreState = field(default_factory=StoreState)


def unpad(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def token_problem(state: AscState, header: str) -> str:
    """Why a request's JWT is not acceptable ("" when it is)."""
    if not header.startswith("Bearer "):
        return "no bearer token"
    try:
        head, body, signature = header[7:].split(".")
        meta, claims = json.loads(unpad(head)), json.loads(unpad(body))
        raw = unpad(signature)
        der = encode_dss_signature(int.from_bytes(raw[:32]), int.from_bytes(raw[32:]))
        state.public_key.verify(der, f"{head}.{body}".encode(), ec.ECDSA(hashes.SHA256()))
    except Exception as err:  # any broken token is a 401
        return f"bad token: {err!r}"
    if meta != {"alg": "ES256", "kid": KEY_ID, "typ": "JWT"}:
        return f"bad header {meta}"
    if claims.get("aud") != "appstoreconnect-v1" or claims.get("iss") != ISSUER_ID:
        return "wrong audience or issuer"
    if not claims["iat"] <= time.time() < claims["exp"] or claims["exp"] - claims["iat"] > 1200:
        return "expired, or valid for more than 20 minutes"
    return ""


def error(status: int, detail: str) -> JSONResponse:
    """An error the way App Store Connect sends them."""
    return JSONResponse({"errors": [{"status": str(status), "title": "error", "detail": detail}]},
                        status_code=status)  # fmt: skip


def standin_app(state: AscState) -> FastAPI:
    """The API as a FastAPI app."""
    app = FastAPI()

    @app.middleware("http")
    async def authenticate(request: Request, call_next: Any) -> Any:
        state.calls.append((request.method, request.url.path))
        if request.url.path.startswith("/upload/"):  # pre-signed, like Apple's upload URLs
            return await call_next(request)
        problem = token_problem(state, request.headers.get("authorization", ""))
        if problem:
            return error(401, problem)
        return await call_next(request)

    @app.get("/v1/apps")
    async def apps(request: Request) -> Any:
        wanted = request.query_params.get("filter[bundleId]")
        found = [a for a in state.apps if wanted in (None, a["attributes"]["bundleId"])]
        return {"data": found, "links": {}}

    @app.get("/v1/bundleIds")
    async def bundle_ids(request: Request) -> Any:
        if state.refuse_identifiers:
            return error(403, "This API key cannot use the Provisioning endpoints.")
        wanted = request.query_params.get("filter[identifier]")
        found = [b for b in state.bundle_ids if wanted in (None, b["attributes"]["identifier"])]
        return {"data": found, "links": {}}

    @app.post("/v1/bundleIds")
    async def new_bundle_id(request: Request) -> Any:
        attributes = (await request.json())["data"]["attributes"]
        identifier = attributes["identifier"]
        if any(b["attributes"]["identifier"] == identifier for b in state.bundle_ids):
            return error(409, f"An App ID with Identifier '{identifier}' is not available.")
        if attributes.get("platform") not in ("IOS", "MAC_OS", "UNIVERSAL"):
            return error(409, "platform must be IOS, MAC_OS or UNIVERSAL")
        made = resource("bundleIds", new_id(), attributes)["data"]
        state.bundle_ids.append(made)
        return {"data": made}

    provisioning_routes(app, state.release, state, error)
    upload_routes(app, state.release, state, error)
    testflight_routes(app, state.release, error)
    library_routes(app, state.store, state.release, state, error)
    return app


class AscStandIn:
    """The stand-in on a free local port, in a background thread."""

    def __init__(self) -> None:
        import uvicorn

        self.pem, public = new_key()
        self.state = AscState(public_key=public)
        config = uvicorn.Config(standin_app(self.state), host="127.0.0.1", port=0,
                                log_level="warning", loop="asyncio")  # fmt: skip
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "AscStandIn":
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("the App Store Connect stand-in did not start")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        self.state.release.base_url = self.url
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(30)

    def add_app(self, name: str, bundle_id: str) -> str:
        """An app record, as the user makes it once in App Store Connect; its id."""
        app_id = str(6400000000 + len(self.state.apps))
        self.state.apps.append({"type": "apps", "id": app_id,
                                "attributes": {"name": name, "bundleId": bundle_id}})  # fmt: skip
        return app_id
