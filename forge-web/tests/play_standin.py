"""A stand-in for Google Play (W37b): Google's OAuth token endpoint and the parts of the Play
Developer API (androidpublisher v3) Forge Web uses, with Google's checks where they matter.

Every token request must carry a JWT signed (RS256) by a service account Google knows, for the
Play scope, addressed to this token endpoint and valid for at most an hour; every API call needs
a token it gave out, and the account must have access to the app. A bundle must be signed, by
the app's upload key once one is known (the first upload sets it, as Play App Signing does), and
its version code must be new. An app never published takes only draft releases. A new personal
account cannot release to production before its closed test.

Tests never call Google. Start it with `with PlayStandIn() as play:` and point
`[stores] play_api_url` and `play_token_url` at `play.url` and `play.token_url`.
"""

import base64
import io
import json
import re
import secrets
import threading
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.serialization import pkcs7
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

SCOPE = "https://www.googleapis.com/auth/androidpublisher"
GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
API = "/androidpublisher/v3/applications/{package}"
CLOSED_TEST = (
    "Your developer account must run a closed test with at least 12 testers for 14 days "
    "before you can publish to production."
)


@dataclass
class PlayApp:
    """An app as Play Console knows it."""

    draft: bool = True  # never published: only draft releases
    upload_sha256: str = ""  # the upload certificate's fingerprint, once known
    version_codes: set[int] = field(default_factory=set)
    tracks: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


@dataclass
class PlayState:
    """What Google knows and what it was sent."""

    token_url: str = ""
    accounts: dict[str, rsa.RSAPublicKey] = field(default_factory=dict)  # email -> its key
    key_ids: dict[str, str] = field(default_factory=dict)  # email -> its key's id
    access: dict[str, set[str]] = field(default_factory=dict)  # email -> packages
    needs_closed_test: set[str] = field(default_factory=set)  # new personal accounts
    apps: dict[str, PlayApp] = field(default_factory=dict)
    tokens: dict[str, tuple[str, float]] = field(default_factory=dict)  # token -> email, until
    edits: dict[str, dict[str, Any]] = field(default_factory=dict)
    calls: list[tuple[str, str]] = field(default_factory=list)
    bundles: list[bytes] = field(default_factory=list)  # every uploaded bundle


def unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def failure(status: int, message: str) -> JSONResponse:
    """An API error the way Google writes it."""
    return JSONResponse({"error": {"code": status, "message": message}}, status_code=status)


def grant_problem(state: PlayState, form: dict[str, str]) -> str:
    """Why a token request is refused ("" when it is fine)."""
    if form.get("grant_type") != GRANT:
        return "Invalid grant_type"
    parts = form.get("assertion", "").split(".")
    if len(parts) != 3:
        return "Invalid JWT"
    try:
        header, claims = json.loads(unb64(parts[0])), json.loads(unb64(parts[1]))
    except ValueError:
        return "Invalid JWT"
    email = str(claims.get("iss", ""))
    key = state.accounts.get(email)
    if header.get("alg") != "RS256" or key is None or header.get("kid") != state.key_ids[email]:
        return "Invalid JWT Signature."
    try:
        key.verify(unb64(parts[2]), f"{parts[0]}.{parts[1]}".encode(), padding.PKCS1v15(),
                   hashes.SHA256())  # fmt: skip
    except InvalidSignature:
        return "Invalid JWT Signature."
    now = time.time()
    if claims.get("scope") != SCOPE or claims.get("aud") != state.token_url:
        return "Invalid JWT: scope or audience"
    if not claims.get("iat", now + 1000) <= now + 60 < claims.get("exp", 0) + 60:
        return "Invalid JWT: Token must be a short-lived token and in a reasonable timeframe"
    if claims["exp"] - claims["iat"] > 3600:
        return "Invalid JWT: Token must be a short-lived token (60 minutes)"
    return ""


def signer_sha256(bundle: bytes) -> str:
    """The fingerprint of the certificate that signed a bundle ("" when it is not signed)."""
    try:
        with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
            blocks = [n for n in archive.namelist() if re.fullmatch(r"META-INF/[^/]+\.RSA", n)]
            if not blocks or "META-INF/MANIFEST.MF" not in archive.namelist():
                return ""
            [certificate] = pkcs7.load_der_pkcs7_certificates(archive.read(blocks[0]))
    except (zipfile.BadZipFile, ValueError):
        return ""
    return certificate.fingerprint(hashes.SHA256()).hex(":").upper()


def version_code_of(bundle: bytes) -> int:
    """The version code in a bundle's manifest (the stand-in host writes it as text)."""
    with zipfile.ZipFile(io.BytesIO(bundle)) as archive:
        manifest = archive.read("base/manifest/AndroidManifest.xml").decode()
    found = re.search(r"versionCode=(\d+)", manifest)
    return int(found.group(1)) if found else 0


def standin_app(state: PlayState) -> FastAPI:
    """The token endpoint and the API."""
    app = FastAPI()

    @app.middleware("http")
    async def signed_in(request: Request, call_next: Any) -> Response:
        state.calls.append((request.method, request.url.path))
        if request.url.path == "/token":
            return await call_next(request)  # type: ignore[no-any-return]
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        email, until = state.tokens.get(token, ("", 0.0))
        if not email or until < time.time():
            return failure(401, "Request had invalid authentication credentials.")
        package = request.url.path.split("/applications/", 1)[-1].split("/", 1)[0]
        if package not in state.apps:
            return failure(404, f"Package not found: {package}.")
        if package not in state.access.get(email, set()):
            return failure(403, "The caller does not have permission")
        request.state.email = email
        return await call_next(request)  # type: ignore[no-any-return]

    @app.post("/token")
    async def token(request: Request) -> Response:
        form = {k: str(v) for k, v in (await request.form()).items()}
        problem = grant_problem(state, form)
        if problem:
            return JSONResponse({"error": "invalid_grant", "error_description": problem}, 400)
        made = secrets.token_urlsafe(24)
        email = json.loads(unb64(form["assertion"].split(".")[1]))["iss"]
        state.tokens[made] = (email, time.time() + 3600)
        return JSONResponse({"access_token": made, "expires_in": 3599, "token_type": "Bearer"})

    edit_routes(app, state)
    return app


def edit_routes(app: FastAPI, state: PlayState) -> None:
    """Edits: open, upload, track, commit, delete."""

    @app.post(API + "/edits")
    async def new_edit(package: str) -> Any:
        edit_id = secrets.token_hex(8)
        state.edits[edit_id] = {"package": package, "bundles": [], "tracks": {}}
        return {"id": edit_id, "expiryTimeSeconds": str(int(time.time()) + 3600)}

    @app.delete(API + "/edits/{edit_id}")
    async def drop(package: str, edit_id: str) -> Response:
        state.edits.pop(edit_id, None)
        return Response(status_code=204)

    @app.post("/upload" + API + "/edits/{edit_id}/bundles")
    async def upload(package: str, edit_id: str, request: Request) -> Any:
        edit, played = state.edits.get(edit_id), state.apps[package]
        if edit is None or request.query_params.get("uploadType") != "media":
            return failure(400, "Invalid request")
        bundle = await request.body()
        state.bundles.append(bundle)
        sha = signer_sha256(bundle)
        if not sha:
            return failure(400, "The Android App Bundle was not signed.")
        if played.upload_sha256 and sha != played.upload_sha256:
            return failure(403, "The Android App Bundle was signed with the wrong key. Found: "
                           f"SHA256: {sha}, expected: SHA256: {played.upload_sha256}")  # fmt: skip
        code = version_code_of(bundle)
        if code in played.version_codes or code in edit["bundles"]:
            return failure(403, "APK specifies a version code that has already been used.")
        edit["bundles"].append(code)
        edit["signer"] = sha
        return {"versionCode": code, "sha256": base64.b64encode(bytes(32)).decode()}

    @app.put(API + "/edits/{edit_id}/tracks/{track}")
    async def track(package: str, edit_id: str, track: str, request: Request) -> Any:
        edit, played, body = state.edits.get(edit_id), state.apps[package], await request.json()
        if edit is None or track not in ("internal", "alpha", "beta", "production"):
            return failure(400, "Invalid request")
        for release in body["releases"]:
            codes = [int(code) for code in release["versionCodes"]]
            if any(code not in edit["bundles"] and code not in played.version_codes
                   for code in codes):  # fmt: skip
                return failure(400, "Version code not found in this edit.")
            if played.draft and release["status"] != "draft":
                return failure(400, "Only releases with status draft may be created on draft app.")
        if track == "production" and request.state.email in state.needs_closed_test:
            return failure(403, CLOSED_TEST)
        edit["tracks"][track] = body["releases"]
        return body

    @app.post(API + "/edits/{edit_id}:commit")
    async def commit(package: str, edit_id: str) -> Any:
        edit, played = state.edits.pop(edit_id, None), state.apps[package]
        if edit is None:
            return failure(404, "This Edit has been deleted.")
        played.version_codes.update(edit["bundles"])
        played.upload_sha256 = played.upload_sha256 or edit.get("signer", "")
        for name, releases in edit["tracks"].items():
            played.tracks.setdefault(name, []).extend(releases)
        return {"id": edit_id}


class PlayStandIn:
    """The stand-in on a free local port, in a background thread."""

    def __init__(self) -> None:
        import uvicorn

        self.state = PlayState()
        config = uvicorn.Config(standin_app(self.state), host="127.0.0.1", port=0,
                                log_level="warning", loop="asyncio")  # fmt: skip
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> "PlayStandIn":
        self.thread.start()
        deadline = time.time() + 30
        while not self.server.started:
            if time.time() > deadline:
                raise RuntimeError("the Google Play stand-in did not start")
            time.sleep(0.02)
        self.url = f"http://127.0.0.1:{self.server.servers[0].sockets[0].getsockname()[1]}"
        self.token_url = self.state.token_url = f"{self.url}/token"
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.should_exit = True
        self.thread.join(30)

    def service_account(self, *packages: str, personal_new: bool = False) -> dict[str, str]:
        """A service account with access to these apps: its JSON key file, as Google gives it."""
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        email = f"forge-{secrets.token_hex(3)}@shop-123.iam.gserviceaccount.com"
        key_id = secrets.token_hex(20)
        self.state.accounts[email], self.state.key_ids[email] = key.public_key(), key_id
        self.state.access[email] = set(packages)
        if personal_new:
            self.state.needs_closed_test.add(email)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()  # fmt: skip
        return {"type": "service_account", "project_id": "shop-123", "private_key_id": key_id,
                "private_key": pem, "client_email": email, "client_id": "1" * 21,
                "token_uri": "https://oauth2.googleapis.com/token"}  # fmt: skip

    def add_app(self, package: str, published: bool = True) -> PlayApp:
        """An app as its maker creates it once in Play Console."""
        self.state.apps[package] = PlayApp(draft=not published)
        return self.state.apps[package]
