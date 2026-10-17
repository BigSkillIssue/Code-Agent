"""What the server and a Windows worker send each other (the server imports only this module).

The same protocol as the Mac worker's: the worker asks for work with `POST /api/win/poll` and
gets a `JobOffer` (or none after a while), downloads the job's input with
`GET /api/win/jobs/<id>/source`, uploads what it made with `POST /api/win/jobs/<id>/package`,
and reports with `POST /api/win/jobs/<id>/result`. Every call carries
`Authorization: Bearer <worker token>`.

A `build` job tests the product's Windows app (`windows/Tests`) and publishes it
(`windows/App`); it runs the product's code. Without a signing machine it also packs the app
with Velopack (unsigned) and sends the release; else it sends the published app (`app.zip`)
and a `pack` job follows on a signing machine: Velopack packs and signs the app there, which
never runs a product's code. A job carries no secrets; a signing machine's own key never leaves
it.
"""

from typing import Literal

from pydantic import BaseModel, Field, model_validator

TOKEN_PREFIX = "fww"
JobKind = Literal["build", "pack"]
MAX_LOG_CHARS = 100_000
VERSION = r"^\d{1,5}\.\d{1,5}\.\d{1,5}$"  # major.minor.patch, as Velopack wants it
PACK_ID = r"^[A-Za-z][A-Za-z0-9]{0,39}$"  # the app's assembly name: its .exe and install folder
TITLE = r"^[^\x00-\x1f\"]{1,100}$"  # what the installer and the Start menu show
Platform = Literal["x64", "arm64"]


class BuildParams(BaseModel):
    """Test and publish the app with this version, for this processor."""

    version: str = Field(pattern=VERSION)
    platform: Platform = "x64"
    title: str = Field(pattern=TITLE)
    pack: bool = True  # pack it here, unsigned (no signing machine takes over)


class PackParams(BaseModel):
    """Pack a published app into its installer, its ZIP and its update files, and sign them."""

    version: str = Field(pattern=VERSION)
    platform: Platform = "x64"
    title: str = Field(pattern=TITLE)
    pack_id: str = Field(pattern=PACK_ID)


class JobOffer(BaseModel):
    """One job for the worker."""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    kind: JobKind
    project: str = Field(pattern=r"^[0-9a-f]{16,64}$")  # a key per project, not its id
    build: BuildParams | None = None
    pack: PackParams | None = None
    timeout_s: float = Field(gt=0, le=6 * 3600)

    @model_validator(mode="after")
    def params_fit(self) -> "JobOffer":
        """Each kind of job has its own parameters."""
        if (self.kind == "build") != (self.build is not None):
            raise ValueError("a build job, and only a build job, has build parameters")
        if (self.kind == "pack") != (self.pack is not None):
            raise ValueError("a pack job, and only a pack job, has pack parameters")
        return self


class PollRequest(BaseModel):
    """The worker has room for more jobs."""

    free_slots: int = Field(ge=1, le=8)
    version: str = Field(default="", max_length=40)


class PollAnswer(BaseModel):
    """A job, or nothing yet (ask again)."""

    job: JobOffer | None = None


class JobResult(BaseModel):
    """How a job ended: its tests, the app's assembly name, and what it sent (`size` bytes)."""

    ok: bool
    error: str = Field(default="", max_length=2_000)
    hint: str = Field(default="", max_length=2_000)
    tests_passed: int = Field(default=0, ge=0)
    tests_failed: int = Field(default=0, ge=0)
    pack_id: str = Field(default="", pattern=r"^(|[A-Za-z][A-Za-z0-9]{0,39})$")
    signed: bool = False  # a signing machine signed what it sent
    size: int = Field(default=0, ge=0)
    log_tail: str = Field(default="", max_length=MAX_LOG_CHARS)
    seconds: float = Field(default=0, ge=0)


def release_files(pack_id: str, version: str) -> dict[str, bool]:
    """The files of an app's Velopack release, and whether each must be there: the installer,
    the ZIP to run without installing, the full package and the list the updates read."""
    return {f"{pack_id}-win-Setup.exe": True, f"{pack_id}-win-Portable.zip": True,
            f"{pack_id}-{version}-full.nupkg": True, "releases.win.json": True,
            "assets.win.json": False, "RELEASES": False}  # fmt: skip
