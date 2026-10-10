"""What the server and a Mac worker send each other (the server imports only this module).

The worker asks for work with `POST /api/mac/poll` and gets a `JobOffer` (or none after a while),
downloads the project with `GET /api/mac/jobs/<id>/source`, may upload an archive with
`POST /api/mac/jobs/<id>/archive`, and reports with `POST /api/mac/jobs/<id>/result`. Every call
carries `Authorization: Bearer <worker token>`.

An export job (W22) gets an archive instead of a project, and its signing certificate and
profiles once from `GET /api/mac/jobs/<id>/signing`; it sends back the signed .ipa or .pkg.
Signing material is never part of a job offer.
"""

import base64
from typing import Literal

from forge.ports import AppleAction, AppleBuildResult, ApplePlatform, AppleScreen
from pydantic import BaseModel, Field, model_validator

TOKEN_PREFIX = "fmw"
JobKind = Literal["build", "screenshot", "export"]
EXPORTS_FROM = (0, 2, 0)  # the first worker version that knows export jobs
STORE_SHOTS_FROM = (0, 3, 0)  # ... and screenshots fitted to an App Store size
MAX_SCREEN_BYTES = 12 * 1024 * 1024
MAX_LOG_CHARS = 100_000
MAX_ISSUES = 500
PNG = b"\x89PNG\r\n\x1a\n"


class BuildParams(BaseModel):
    """Build, test or archive for one platform."""

    platform: ApplePlatform
    action: AppleAction = "build"
    scheme: str | None = Field(default=None, max_length=200)
    # An archive for the App Store (W22): signed ad hoc, with this build number.
    build_number: int | None = Field(default=None, ge=1, le=10**12)


class StoreSize(BaseModel):
    """An exact size the App Store takes for a screenshot, in pixels."""

    width: int = Field(ge=100, le=8000)
    height: int = Field(ge=100, le=8000)


class ScreenshotParams(BaseModel):
    """Start the app on a device (or the Mac) and take a picture; with `fit`, made exactly that
    size for the App Store (scaled, padded, without transparency)."""

    platform: ApplePlatform
    device: str | None = Field(default=None, max_length=200)
    dark: bool = False
    fit: StoreSize | None = None


class ExportParams(BaseModel):
    """Sign an archive for the App Store and export it (.ipa for iOS, .pkg for the Mac)."""

    platform: Literal["ios", "macos"]
    team_id: str = Field(pattern=r"^[A-Z0-9]{10}$")
    # Which profile signs which bundle (the app, its Watch app), by the profile's name.
    profiles: dict[str, str] = Field(max_length=10)


class Profile(BaseModel):
    """A provisioning profile, as App Store Connect makes it."""

    name: str = Field(max_length=200)
    uuid: str = Field(pattern=r"^[0-9A-Fa-f-]{36}$")
    data_b64: str = Field(max_length=200_000)


class SigningMaterial(BaseModel):
    """What one export needs to sign, and nothing more: the distribution certificate with its
    key (and for the Mac the installer's) as password-protected .p12, and the profiles."""

    p12_b64: str = Field(max_length=100_000)
    installer_p12_b64: str = Field(default="", max_length=100_000)
    password: str = Field(min_length=16, max_length=200)
    profiles: list[Profile] = Field(max_length=10)


class ExportResult(BaseModel):
    """How an export went; the file itself is sent like an archive."""

    ok: bool
    platform: Literal["ios", "macos"]
    file_name: str = Field(default="", max_length=200)
    size: int = Field(default=0, ge=0)
    log_tail: str = Field(default="", max_length=MAX_LOG_CHARS)
    artifact: str = ""  # on the server: "job:<id>" once the file arrived


class JobOffer(BaseModel):
    """One job for the worker."""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: JobKind
    project: str = Field(pattern=r"^[0-9a-f]{16,64}$")  # a key per project, not its id
    build: BuildParams | None = None
    screenshot: ScreenshotParams | None = None
    export: ExportParams | None = None
    timeout_s: float = Field(gt=0)

    @model_validator(mode="after")
    def params_fit(self) -> "JobOffer":
        """Each kind of job has its own parameters, and only those."""
        given = {"build": self.build is not None, "screenshot": self.screenshot is not None,
                 "export": self.export is not None}  # fmt: skip
        if not given[self.kind] or sum(given.values()) != 1:
            raise ValueError(f"a {self.kind} job needs {self.kind} parameters (and only those)")
        return self


class PollRequest(BaseModel):
    """The worker has room for more jobs."""

    free_slots: int = Field(ge=1, le=8)
    version: str = Field(default="", max_length=40)


class PollAnswer(BaseModel):
    """A job, or nothing yet (ask again)."""

    job: JobOffer | None = None


class JobResult(BaseModel):
    """How a job ended. `ok` is False when it could not run at all (no VM, no Xcode); a build
    that ran and failed is ok with a failed `build`."""

    ok: bool
    error: str = Field(default="", max_length=2_000)
    hint: str = Field(default="", max_length=2_000)
    build: AppleBuildResult | None = None
    screen: AppleScreen | None = None
    export: ExportResult | None = None
    seconds: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def bounded(self) -> "JobResult":
        """Results come from a Mac that ran someone's code: keep them small and well formed."""
        if self.build is not None:
            if len(self.build.issues) > MAX_ISSUES:
                raise ValueError(f"at most {MAX_ISSUES} build messages")
            if len(self.build.log_tail) > MAX_LOG_CHARS:
                raise ValueError(f"the log tail is longer than {MAX_LOG_CHARS} characters")
        if self.screen is not None:
            check_png(self.screen.image.data_b64)
        return self


def check_png(data_b64: str) -> None:
    """A screenshot must be a PNG of reasonable size."""
    if len(data_b64) > MAX_SCREEN_BYTES * 4 // 3 + 4:
        raise ValueError("the screenshot is too large")
    try:
        head = base64.b64decode(data_b64[:16], validate=True)
    except ValueError:
        raise ValueError("the screenshot is not base64") from None
    if not head.startswith(PNG):
        raise ValueError("the screenshot is not a PNG")


def knows_exports(version: str) -> bool:
    """Whether a worker of this version can run export jobs."""
    return version_of(version) >= EXPORTS_FROM


def knows_store_shots(version: str) -> bool:
    """Whether a worker of this version can fit screenshots to an App Store size."""
    return version_of(version) >= STORE_SHOTS_FROM


def version_of(version: str) -> tuple[int, ...]:
    """A worker's version as numbers ((0,) when it sent none or nonsense)."""
    try:
        return tuple(int(part) for part in version.split(".")[:3])
    except ValueError:
        return (0,)
