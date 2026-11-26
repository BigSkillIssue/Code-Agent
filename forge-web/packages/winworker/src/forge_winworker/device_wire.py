"""What Forge Web and a worker say about a device session (W60): the product's app running on a
device the agent works with (an Android emulator on a host, a Windows Sandbox on a Windows
machine), and the actions the agent takes on it.

The worker asks for a session (long poll), fetches its packed source, builds and starts the app,
and answers action 0 with the first screen once it runs. Then it asks for the next action (long
poll) and answers each one, until the action is `stop` or the session's time is up.

Forge Web's `forge_hostworker.device_wire` and `forge_winworker.device_wire` are the same text,
so neither worker depends on the other (a test keeps them equal).
"""

from typing import Literal

from pydantic import BaseModel, Field

DevicePlatform = Literal["android", "windows"]
ActionKind = Literal["tap", "type", "swipe", "key", "screenshot", "read", "stop"]
Key = Literal["back", "home", "enter", "tab", "escape", "menu", "delete"]
Direction = Literal["up", "down", "left", "right"]
MAX_TEXT = 50_000  # characters of a UI tree or a message handed back
MAX_IMAGE_B64 = 8_000_000  # a screenshot, base64


class DeviceOffer(BaseModel):
    """A session for this worker: which app, built how, for how long."""

    id: str = Field(pattern=r"^[0-9a-f]{32}$")
    platform: DevicePlatform
    project: str = Field(pattern=r"^[0-9a-f]{32}$")  # a hash, never the project's id
    app: str = Field(default="", pattern=r"^$|^[a-z0-9][a-z0-9-]{0,62}$")  # its name on its host
    # its application ID (Android; "": find it), never more than a Java package name
    package: str = Field(
        default="", max_length=255, pattern=r"^$|^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$"
    )
    build_image: str = Field(default="", max_length=400)  # Gradle's image (Android)
    device_image: str = Field(default="", max_length=400)  # the emulator's image (Android)
    start_timeout_s: float = Field(default=900, gt=0, le=7200)  # build, boot and launch
    max_s: float = Field(default=1800, gt=0, le=14_400)  # the whole session


class DevicePoll(BaseModel):
    """A session, or nothing yet (ask again)."""

    session: DeviceOffer | None = None


class DeviceAction(BaseModel):
    """One thing the agent does on the device. Points are pixels of the last screenshot."""

    seq: int = Field(ge=1)
    kind: ActionKind
    x: int | None = Field(default=None, ge=0, le=20_000)
    y: int | None = Field(default=None, ge=0, le=20_000)
    target: str = Field(default="", max_length=500)  # visible text or accessibility name
    text: str = Field(default="", max_length=5_000)
    submit: bool = False  # press Enter after typing
    direction: Direction | None = None
    key: Key | None = None


class DeviceNext(BaseModel):
    """The next action, or nothing yet (ask again)."""

    action: DeviceAction | None = None
    ended: bool = False  # the session is over: stop the device


class DeviceAnswer(BaseModel):
    """What an action did: a line of text and the screen afterwards (seq 0: the app started)."""

    seq: int = Field(ge=0)
    ok: bool
    text: str = Field(default="", max_length=MAX_TEXT)
    image_b64: str = Field(default="", max_length=MAX_IMAGE_B64)
    media_type: Literal["image/png", "image/jpeg"] = "image/png"
    width: int = Field(default=0, ge=0, le=20_000)
    height: int = Field(default=0, ge=0, le=20_000)
    error: str = Field(default="", max_length=2_000)
    hint: str = Field(default="", max_length=2_000)
