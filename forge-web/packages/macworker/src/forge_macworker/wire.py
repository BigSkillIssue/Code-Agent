"""What the server and a Mac worker send each other (the server imports only this module).

The worker asks for work with `POST /api/mac/poll` and gets a `JobOffer` (or none after a while),
downloads the project with `GET /api/mac/jobs/<id>/source`, may upload an archive with
`POST /api/mac/jobs/<id>/archive`, and reports with `POST /api/mac/jobs/<id>/result`. Every call
carries `Authorization: Bearer <worker token>`.
"""

import base64
from typing import Literal

from forge.ports import AppleAction, AppleBuildResult, ApplePlatform, AppleScreen
from pydantic import BaseModel, Field, model_validator

TOKEN_PREFIX = "fmw"
JobKind = Literal["build", "screenshot"]
MAX_SCREEN_BYTES = 12 * 1024 * 1024
MAX_LOG_CHARS = 100_000
MAX_ISSUES = 500
PNG = b"\x89PNG\r\n\x1a\n"


class BuildParams(BaseModel):
    """Build, test or archive for one platform."""

    platform: ApplePlatform
    action: AppleAction = "build"
    scheme: str | None = Field(default=None, max_length=200)


class ScreenshotParams(BaseModel):
    """Start the app on a device (or the Mac) and take a picture."""

    platform: ApplePlatform
    device: str | None = Field(default=None, max_length=200)
    dark: bool = False


class JobOffer(BaseModel):
    """One job for the worker."""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: JobKind
    project: str = Field(pattern=r"^[0-9a-f]{16,64}$")  # a key per project, not its id
    build: BuildParams | None = None
    screenshot: ScreenshotParams | None = None
    timeout_s: float = Field(gt=0)

    @model_validator(mode="after")
    def params_fit(self) -> "JobOffer":
        """A build job has build parameters, a screenshot job screenshot parameters."""
        given = {"build": self.build is not None, "screenshot": self.screenshot is not None}
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
