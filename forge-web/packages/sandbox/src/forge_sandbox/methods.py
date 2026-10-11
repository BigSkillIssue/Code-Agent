"""Parameters of the daemon's control-channel methods (shared by the daemon and the server)."""

from collections.abc import Awaitable, Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from forge_sandbox.rpc import Handler, RpcError

READ_LIMIT = 900_000


class Params(BaseModel):
    """Base of every parameter model: unknown keys are an error."""

    model_config = ConfigDict(extra="forbid")


class PathParams(Params):
    """A workspace-relative path ("" is the root)."""

    path: str = ""


class ReadParams(PathParams):
    """fs.read: how much to read, from where."""

    limit: int = Field(default=READ_LIMIT, ge=0, le=READ_LIMIT)
    offset: int = Field(default=0, ge=0)


class WritePartParams(PathParams):
    """fs.write_part: one part of a large file (base64); `last` puts the file in place."""

    upload: str = Field(pattern=r"^[0-9a-f]{16}$")
    base64: str = ""
    last: bool = False
    create_dirs: bool = False
    abort: bool = False


class WriteParams(PathParams):
    """fs.write: the content as text or base64."""

    text: str | None = None
    base64: str | None = None
    create_dirs: bool = False
    expected_mtime: float | None = None


class UnzipParams(PathParams):
    """fs.unzip: the archive (`path`), where to unpack it, and whether to drop one top folder."""

    dest: str = ""
    strip_root: bool = True
    max_bytes: int | None = Field(default=None, ge=0)  # unpack at most this much


class DeleteParams(PathParams):
    """fs.delete: also non-empty directories?"""

    recursive: bool = False


class RenameParams(Params):
    """fs.rename: from where to where."""

    src: str
    dst: str


class ProcStartParams(Params):
    """procs.start: an argv list or a shell command line."""

    argv: list[str] | None = Field(default=None, max_length=256)
    command: str | None = Field(default=None, max_length=8192)
    cwd: str = ""
    env: dict[str, str] = Field(default_factory=dict, max_length=64)
    name: str = Field(default="", max_length=120)


class ProcParams(Params):
    """A process id."""

    id: str


class ProcOutputParams(ProcParams):
    """procs.output: lines from `since` on."""

    since: int = Field(default=0, ge=0)
    limit: int = Field(default=500, ge=1, le=5000)


class PtyCreateParams(Params):
    """pty.create: size, folder and program of a new terminal."""

    cols: int = Field(default=80, ge=10, le=500)
    rows: int = Field(default=24, ge=4, le=200)
    cwd: str = ""
    argv: list[str] | None = Field(default=None, max_length=64)


class PtyResizeParams(Params):
    """pty.resize: the new size."""

    id: str
    cols: int = Field(ge=10, le=500)
    rows: int = Field(ge=4, le=200)


class ListenParams(Params):
    """forward.listen: a TCP port inside the sandbox that leads to a server target."""

    target: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")
    port: int = Field(ge=0, le=65535)


class GitDiffParams(Params):
    """git.diff: of one path or everything; staged or not."""

    path: str | None = None
    staged: bool = False
    limit: int = Field(default=800_000, ge=1, le=READ_LIMIT)


class GitFilesParams(Params):
    """git.files: the project's files (without ignored ones), best matches for `query` first."""

    query: str = Field(default="", max_length=200)
    limit: int = Field(default=50, ge=1, le=1000)


class GitPathsParams(Params):
    """git.stage, git.unstage, git.discard: which paths."""

    paths: list[str] = Field(min_length=1, max_length=1000)


class GitCommitParams(Params):
    """git.commit: the message and who commits."""

    message: str = Field(min_length=1, max_length=10_000)
    name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)


class GitSwitchParams(Params):
    """git.switch: to which branch; `create` makes it from the current one."""

    branch: str = Field(min_length=1, max_length=200)
    create: bool = False


class GitBranchParams(Params):
    """git.bundle_out, git.bundle_in: which branch."""

    branch: str = Field(min_length=1, max_length=200)


class GitArchiveParams(Params):
    """git.archive: which commit."""

    commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")


class GitCloneParams(Params):
    """git.bundle_clone: the URL `origin` gets."""

    url: str = Field(min_length=1, max_length=2000)


class GitLogParams(Params):
    """git.log: how many commits."""

    limit: int = Field(default=50, ge=1, le=500)


class GitRemoteParams(Params):
    """git.set_remote: the URL of `origin`."""

    url: str = Field(min_length=1, max_length=2000)


class EmptyParams(Params):
    """A method without parameters."""


class PortsParams(Params):
    """ports.list: with `owned`, only ports of programs the daemon started (in local mode the
    host's other programs listen too)."""

    owned: bool = False


class ConnectArgs(Params):
    """Arguments of a `connect` channel: a port on the sandbox's loopback interface."""

    port: int = Field(ge=1, le=65535)


class ForwardArgs(Params):
    """Arguments of a `forward` channel the daemon opens: which server target."""

    target: str = Field(pattern=r"^[a-z][a-z0-9-]{0,30}$")


class PtyAttachArgs(Params):
    """Arguments of a `pty` channel: which terminal."""

    id: str


class PtyResize(BaseModel):
    """A message on a `pty` channel that changes the terminal size."""

    type: Literal["resize"] = "resize"
    cols: int = Field(ge=10, le=500)
    rows: int = Field(ge=4, le=200)


CHAT_ID = r"^[a-z0-9][a-z0-9-]{0,63}$"
ChatMode = Literal["ask", "edits", "auto"]


class ChatOptions(Params):
    """How a chat's worker runs Forge: approvals, models, endpoints, limits."""

    mode: ChatMode = "edits"  # ask: approve every change; edits: changes run, commands ask; auto
    model: str | None = None  # "provider/model" for every role
    roles: dict[str, list[str]] = Field(default_factory=dict)  # per-role fallback chains
    providers: dict[str, dict[str, Any]] = Field(default_factory=dict)  # name -> ProviderConfig
    sandbox_mode: Literal["read-only", "workspace-write", "full-access"] = "workspace-write"
    max_cost_usd: float | None = Field(default=None, ge=0)
    fake_script: dict[str, Any] | None = None  # FakeProvider turns (development and tests)
    # Apple projects: where the server's Mac build service is (the gateway, inside the
    # sandbox), the variable holding the run token, and whether Apple's guidelines are checked.
    apple_url: str | None = Field(default=None, pattern=r"^http://127\.0\.0\.1:\d+$")
    apple_token_env: str = Field(default="FW_GATEWAY_TOKEN", pattern=r"^[A-Z][A-Z0-9_]{0,63}$")
    apple_max_mb: int = Field(default=300, ge=1, le=100_000)
    apple_review: bool = False
    # App projects: the release review, the blueprint and the go-live question (Forge S66-S68).
    app_review: bool = False


class ChatParams(Params):
    """A chat id."""

    chat_id: str = Field(pattern=CHAT_ID)


class ChatOpenParams(ChatParams):
    """chat.open: start (or restart) a chat's worker with these options and extra variables."""

    options: ChatOptions = Field(default_factory=ChatOptions)
    env: dict[str, str] = Field(default_factory=dict, max_length=32)


class ChatSendParams(ChatParams):
    """chat.send: a prompt, or a slash command when it starts with '/'."""

    text: str = Field(min_length=1, max_length=100_000)


class ChatAnswerParams(ChatParams):
    """chat.answer: the answer to an approval or a question the worker asked."""

    request_id: str = Field(max_length=64)
    answer: dict[str, Any]


class ChatAttachArgs(Params):
    """Arguments of a `chat` channel: which chat, and the last item the server already has."""

    chat_id: str = Field(pattern=CHAT_ID)
    after_seq: int = Field(default=0, ge=0)


def method[M: Params](model: type[M], run: Callable[[M], Awaitable[Any]]) -> Handler:
    """A handler that validates its parameters with `model` before calling `run`."""

    async def handler(params: dict[str, Any]) -> Any:
        return await run(parse_params(model, params))

    return handler


def parse_params[M: BaseModel](model: type[M], params: dict[str, Any]) -> M:
    """`params` as `model`; RpcError("bad_params") naming the first problem."""
    try:
        return model.model_validate(params)
    except ValidationError as err:
        first = err.errors()[0]
        where = ".".join(str(part) for part in first["loc"]) or "params"
        raise RpcError("bad_params", f"{where}: {first['msg']}") from None
