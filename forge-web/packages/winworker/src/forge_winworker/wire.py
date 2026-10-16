"""What the server and a Windows worker send each other (the server imports only this module).

The same protocol as the Mac worker's: the worker asks for work with `POST /api/win/poll` and
gets a `JobOffer` (or none after a while), downloads the packed commit with
`GET /api/win/jobs/<id>/source`, uploads the package with `POST /api/win/jobs/<id>/package`,
and reports with `POST /api/win/jobs/<id>/result`. Every call carries
`Authorization: Bearer <worker token>`.

An `msix` job tests the product's Windows app (`windows/Tests`) and packages it for the
Microsoft Store (`windows/App`, an unsigned `.msixupload`: the Store signs it). A job carries no
secrets.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

TOKEN_PREFIX = "fww"
JobKind = Literal["msix"]
MAX_LOG_CHARS = 100_000
VERSION = r"^\d{1,5}\.\d{1,5}\.\d{1,5}\.0$"  # the Store wants the last part 0


class MsixParams(BaseModel):
    """Test and package the app with this version, for this processor."""

    version: str = Field(pattern=VERSION)
    platform: Literal["x64", "arm64"] = "x64"


class JobOffer(BaseModel):
    """One job for the worker."""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: JobKind
    project: str = Field(pattern=r"^[0-9a-f]{16,64}$")  # a key per project, not its id
    msix: MsixParams | None = None
    timeout_s: float = Field(gt=0, le=6 * 3600)

    @model_validator(mode="after")
    def params_fit(self) -> "JobOffer":
        """Each kind of job has its own parameters."""
        if self.kind == "msix" and self.msix is None:
            raise ValueError("an msix job needs msix parameters")
        return self


class PollRequest(BaseModel):
    """The worker has room for more jobs."""

    free_slots: int = Field(ge=1, le=8)
    version: str = Field(default="", max_length=40)


class PollAnswer(BaseModel):
    """A job, or nothing yet (ask again)."""

    job: JobOffer | None = None


class JobResult(BaseModel):
    """How a job ended: its tests, and the package it sent (`file_name`, `size`)."""

    ok: bool
    error: str = Field(default="", max_length=2_000)
    hint: str = Field(default="", max_length=2_000)
    tests_passed: int = Field(default=0, ge=0)
    tests_failed: int = Field(default=0, ge=0)
    file_name: str = Field(default="", max_length=200, pattern=r"^$|^[\w.-]+\.msixupload$")
    size: int = Field(default=0, ge=0)
    log_tail: str = Field(default="", max_length=MAX_LOG_CHARS)
    seconds: float = Field(default=0, ge=0)
