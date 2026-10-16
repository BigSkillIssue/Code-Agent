"""The Microsoft Store submission API (Partner Center), with a user's Entra ID app (W38b).

The maker registers an app in Microsoft Entra ID, links it to their Partner Center account as a
manager, and gives Forge Web its tenant, client ID and a client secret; the secret never leaves
this server. A submission is: a new submission (a copy of the last published one), the old
packages marked for deletion and the new one added, the package uploaded as a ZIP to the
submission's blob URL, the submission committed, its status followed. Microsoft's error
messages come back as `StoreError` in Microsoft's own words.
"""

import asyncio
import time
import zipfile
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

RESOURCE = "https://manage.devcenter.microsoft.com"
GUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
STORE_ID = r"^[0-9A-Z]{12}$"  # the app's Store ID in Partner Center, e.g. 9NBLGGH4R315
UPLOADED = "package.msixupload"  # the package's name inside the uploaded ZIP


class StoreError(Exception):
    """The Microsoft Store refused a request (or could not be reached)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


class EntraApp(BaseModel):
    """What Forge Web keeps of a maker's Entra ID app."""

    model_config = ConfigDict(extra="forbid")
    tenant_id: str = Field(pattern=GUID)
    client_id: str = Field(pattern=GUID)
    client_secret: str = Field(min_length=8, max_length=200)


def microsoft_message(reply: httpx.Response) -> str:
    """Microsoft's own explanation of an error, or the status line."""
    try:
        found = reply.json()
    except ValueError:
        found = {}
    if not isinstance(found, dict):
        found = {}
    error = found.get("error")
    if isinstance(error, dict):  # the submission API: {"error": {"code": ..., "message": ...}}
        text = str(error.get("message") or error.get("code") or "")
    else:  # Entra ID: {"error": "invalid_client", "error_description": "AADSTS..."}
        text = str(found.get("error_description") or error or found.get("message") or "")
    return text[:1000] or f"the Microsoft Store answered {reply.status_code}"


def zipped(package: Path) -> Path:
    """The ZIP the submission's blob takes (next to the package): the package under the name
    the submission lists."""
    target = package.with_suffix(".upload.zip")
    with zipfile.ZipFile(target, "w", zipfile.ZIP_STORED) as archive:
        archive.write(package, UPLOADED)
    return target


def blob_problem(url: str, api_url: str) -> str:
    """Why a blob URL from the API is not one to upload to ("" when it is): Azure storage, or
    the API's own host (the stand-in)."""
    found, api = httpx.URL(url), httpx.URL(api_url)
    if found.host == api.host and found.scheme == api.scheme:
        return ""
    if found.scheme == "https" and found.host.endswith(".blob.core.windows.net"):
        return ""
    return f"the Microsoft Store named an upload address that is not Azure storage: {found.host}"


async def chunks(path: Path, size: int = 1024 * 1024) -> AsyncIterator[bytes]:
    """A file in parts, read off the event loop."""
    with path.open("rb") as handle:
        while part := await asyncio.to_thread(handle.read, size):
            yield part


def with_package(submission: dict[str, Any]) -> dict[str, Any]:
    """The submission with its earlier packages marked for deletion and the new one added."""
    packages = [dict(p, fileStatus="PendingDelete")
                for p in submission.get("applicationPackages", [])]  # fmt: skip
    packages.append({"fileName": UPLOADED, "fileStatus": "PendingUpload",
                     "minimumDirectXVersion": "None", "minimumSystemRam": "None"})  # fmt: skip
    return {**submission, "applicationPackages": packages}


@dataclass
class StoreClient:
    """Calls the submission API as one Entra ID app."""

    api_url: str
    login_url: str
    app: EntraApp
    http: httpx.AsyncClient = field(
        default_factory=lambda: httpx.AsyncClient(timeout=httpx.Timeout(600, connect=20))
    )
    _token: tuple[str, float] = ("", 0.0)

    async def bearer(self) -> str:
        """The current access token, renewed five minutes before it runs out."""
        now = time.time()
        if now > self._token[1] - 300:
            url = f"{self.login_url.rstrip('/')}/{self.app.tenant_id}/oauth2/token"
            form = {"grant_type": "client_credentials", "client_id": self.app.client_id,
                    "client_secret": self.app.client_secret, "resource": RESOURCE}  # fmt: skip
            answer = await self.call("POST", url, data=form, signed=False)
            token = str(answer.get("access_token", ""))
            if not token:
                raise StoreError(0, "Microsoft gave no access token")
            self._token = (token, now + float(answer.get("expires_in", 3600)))
        return self._token[0]

    async def call(self, method: str, url: str, signed: bool = True, **kwargs: Any) -> Any:
        """One request; the JSON answer (empty for no content)."""
        headers = {"Accept": "application/json"} | kwargs.pop("headers", {})
        if signed:
            headers["Authorization"] = f"Bearer {await self.bearer()}"
        try:
            reply = await self.http.request(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as err:
            raise StoreError(0, f"the Microsoft Store is not reachable: {err}") from None
        if reply.status_code >= 400:
            raise StoreError(reply.status_code, microsoft_message(reply))
        return reply.json() if reply.content else {}

    def app_url(self, store_id: str) -> str:
        return f"{self.api_url.rstrip('/')}/v1.0/my/applications/{store_id}"

    async def submit(self, store_id: str, package: Path) -> str:
        """A new submission with this package, uploaded and committed; its id."""
        app = await self.call("GET", self.app_url(store_id))
        if app.get("pendingApplicationSubmission"):
            raise StoreError(409, "a submission of this app is in progress in Partner Center")
        made = await self.call("POST", f"{self.app_url(store_id)}/submissions")
        submission_id, blob = str(made["id"]), str(made["fileUploadUrl"])
        url = f"{self.app_url(store_id)}/submissions/{submission_id}"
        problem = blob_problem(blob, self.api_url)
        if problem:
            raise StoreError(0, problem)
        await self.call("PUT", url, json=with_package(made))
        upload = await asyncio.to_thread(zipped, package)
        try:
            await self.call("PUT", blob, signed=False, content=chunks(upload),
                            headers={"x-ms-blob-type": "BlockBlob",
                                     "Content-Length": str(upload.stat().st_size)})  # fmt: skip
        finally:
            upload.unlink(missing_ok=True)
        await self.call("POST", f"{url}/commit")
        return submission_id

    async def status(self, store_id: str, submission_id: str) -> tuple[str, list[str]]:
        """A submission's status and Microsoft's errors about it."""
        found = await self.call(
            "GET", f"{self.app_url(store_id)}/submissions/{submission_id}/status"
        )
        details = found.get("statusDetails") or {}
        errors = [f"{e.get('code', '')}: {e.get('details', '')}".strip(": ")
                  for e in details.get("errors", []) if isinstance(e, dict)]  # fmt: skip
        return str(found.get("status", "")), errors

    async def check(self) -> str:
        """Whether Microsoft takes the app's secret, in one sentence (StoreError when not)."""
        await self.bearer()
        return ("The Entra ID app works: Microsoft gave it an access token. Whether it may "
                "submit an app shows at that app's first submission.")  # fmt: skip

    async def close(self) -> None:
        await self.http.aclose()
