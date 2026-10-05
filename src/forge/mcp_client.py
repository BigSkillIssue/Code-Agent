"""MCP client: connect `[mcp_servers.*]` and offer their tools as `mcp__<server>__<tool>`.

Each server connection lives in its own asyncio task (the SDK's transports are context
managers that must be entered and left in the same task). The tools run through the normal
`call_tool` stages; they are kept per session in an `McpHub`, never in the global registry.
"""

import asyncio
import os
import re
from contextlib import AsyncExitStack
from functools import partial
from pathlib import Path
from typing import Any, TextIO

import httpx2
from mcp import ClientSession, types
from mcp.client.stdio import StdioServerParameters, get_default_environment, stdio_client
from mcp.client.streamable_http import streamable_http_client

from forge.config import ForgeConfig, McpServerConfig
from forge.ctx import Ctx
from forge.providers.base import ImagePart, ToolResult, ToolSpec
from forge.runtime.errors import ToolError, failure
from forge.runtime.web import size_text
from forge.tools import ToolDef, model_has_vision

TOOL_TIMEOUT_S = 120.0
CONNECT_TIMEOUT_S = 30.0
NAME_CHARS = re.compile(r"[^a-zA-Z0-9_-]")
JSON_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "integer": (int,),
    "number": (int, float),
    "boolean": (bool,),
    "array": (list,),
    "object": (dict,),
}
WORD = re.compile(r"[a-z0-9]+")


class McpConnection:
    """One server: a task that holds the connection open until close()."""

    def __init__(self, name: str, config: McpServerConfig, root: Path) -> None:
        self.name, self.config, self.root = name, config, root
        self.session: ClientSession | None = None
        self.error: BaseException | None = None
        self._ready = asyncio.Event()
        self._closing = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> ClientSession:
        """Connect and initialize; raises when the server cannot be reached."""
        self._ready.clear()
        self._closing.clear()
        self.error = None
        self._task = asyncio.create_task(self._serve())
        await asyncio.wait_for(self._ready.wait(), CONNECT_TIMEOUT_S)
        if self.session is None:
            raise ConnectionError(f"MCP server {self.name}: {self.error or 'no connection'}")
        return self.session

    async def _serve(self) -> None:
        try:
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(self._transport(stack))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                self.session = session
                self._ready.set()
                await self._closing.wait()
        except Exception as exc:  # reported by start() / the next call, never raised here
            self.error = exc
        finally:
            self.session = None
            self._ready.set()

    def _transport(self, stack: AsyncExitStack) -> Any:
        config = self.config
        if config.command:
            env = {
                **get_default_environment(),
                **{k: os.environ[k] for k in config.env_keys if k in os.environ},
            }
            params = StdioServerParameters(
                command=config.command[0], args=config.command[1:], env=env, cwd=self.root
            )
            return stdio_client(params, errlog=self._errlog(stack))
        headers = {header: os.environ.get(var, "") for header, var in config.headers_env.items()}
        return streamable_http_client(
            config.url or "", http_client=httpx2.AsyncClient(headers=headers)
        )

    def _errlog(self, stack: AsyncExitStack) -> TextIO:
        folder = self.root / ".forge" / "mcp"
        folder.mkdir(parents=True, exist_ok=True)
        return stack.enter_context((folder / f"{self.name}.log").open("a", encoding="utf-8"))

    @property
    def alive(self) -> bool:
        """True while the connection is up."""
        return self.session is not None and self._task is not None and not self._task.done()

    async def close(self) -> None:
        """End the connection (and the server process)."""
        self._closing.set()
        if self._task is not None:
            await asyncio.wait({self._task}, timeout=10)


class McpHub:
    """All MCP servers of a session and the tools they offer."""

    def __init__(self, cfg: ForgeConfig, root: Path) -> None:
        self.cfg = cfg
        self.connections = {
            name: McpConnection(name, conf, root) for name, conf in cfg.mcp_servers.items()
        }
        self.tools: dict[str, ToolDef] = {}
        self.loaded: set[str] = set()
        self.deferred = False

    async def connect(self) -> list[str]:
        """Connect every server and list its tools; returns one error line per failed server."""
        errors: list[str] = []
        for name, connection in self.connections.items():
            try:
                session = await connection.start()
                listing = await session.list_tools()
            except Exception as exc:
                errors.append(f"MCP server {name} is not available: {exc}")
                continue
            for tool in listing.tools:
                self.add_tool(name, tool)
        self.deferred = len(self.tools) > self.cfg.limits.mcp_defer_threshold
        return errors

    def add_tool(self, server: str, tool: types.Tool) -> None:
        """Register one server tool as a ToolDef."""
        name = NAME_CHARS.sub("_", f"mcp__{server}__{tool.name}")[:64]
        schema = dict(tool.input_schema or {"type": "object", "properties": {}})
        spec = ToolSpec(name=name, description=tool.description or tool.name, parameters=schema)
        read_only = bool(tool.annotations and tool.annotations.read_only_hint)
        fn = partial(self.call, server, tool.name, schema)
        self.tools[name] = ToolDef(name, "mcp", fn, spec, "ask", read_only, None)

    # ------------------------------------------------------------------ the McpTools port

    def tool(self, name: str) -> ToolDef | None:
        """An MCP tool by its Forge name."""
        return self.tools.get(name)

    def visible_tools(self) -> list[ToolDef]:
        """Tools whose schemas the model gets: all, or only the loaded ones in deferred mode."""
        return [t for name, t in self.tools.items() if not self.deferred or name in self.loaded]

    def deferred_listing(self) -> list[str]:
        """`name: first sentence` for every tool not loaded yet (deferred mode only)."""
        if not self.deferred:
            return []
        return [
            f"{t.name}: {first_sentence(t.spec.description)}"
            for n, t in self.tools.items()
            if n not in self.loaded
        ]

    def servers(self) -> list[str]:
        """Names of the configured servers."""
        return list(self.connections)

    def search(self, query: str, limit: int) -> list[ToolDef]:
        """Load the best-matching deferred tools (name matches count double)."""
        words = set(WORD.findall(query.lower()))
        scored: list[tuple[int, str]] = []
        for name, tool in self.tools.items():
            if name in self.loaded:
                continue
            in_name = words & set(WORD.findall(name.lower()))
            in_text = words & set(WORD.findall(tool.spec.description.lower()))
            score = 2 * len(in_name) + len(in_text)
            if score > 0:
                scored.append((score, name))
        scored.sort(key=lambda item: (-item[0], item[1]))
        picked = [self.tools[name] for _, name in scored[:limit]]
        self.loaded.update(t.name for t in picked)
        return picked

    async def call(
        self, server: str, tool: str, schema: dict[str, Any], ctx: Ctx, **arguments: Any
    ) -> ToolResult:
        """Run a server tool (the body of every MCP ToolDef)."""
        check_arguments(schema, arguments)
        result = await self.request(
            server, lambda s: s.call_tool(tool, arguments, read_timeout_seconds=TOOL_TIMEOUT_S)
        )
        if not isinstance(result, types.CallToolResult):
            raise ToolError("tool_error", f"{tool} asked for input, which Forge does not support")
        text, images = convert(result.content, model_has_vision(ctx))
        if result.is_error:
            return failure("tool_error", f"{tool} reported an error", body=text)
        return ToolResult(call_id="", ok=True, text=text or "(no output)", images=images)

    async def request(self, server: str, send: Any) -> Any:
        """Send a request, reconnecting once if the connection was lost."""
        connection = self.connections.get(server)
        if connection is None:
            raise ToolError(
                "not_found",
                f"no MCP server '{server}'",
                hint="servers: " + ", ".join(self.connections),
            )
        for attempt in (1, 2):
            if not connection.alive:
                try:
                    await connection.start()
                except Exception as exc:
                    raise ToolError(
                        "network", f"MCP server {server} is not reachable: {exc}"
                    ) from exc
            assert connection.session is not None
            try:
                return await asyncio.wait_for(send(connection.session), TOOL_TIMEOUT_S + 5)
            except TimeoutError as exc:
                raise ToolError("timeout", f"MCP server {server} did not answer in time") from exc
            except Exception as exc:
                if connection.alive or attempt == 2:
                    raise ToolError("tool_error", f"MCP server {server}: {exc}") from exc
        raise AssertionError("unreachable")

    async def list_resources(self, server: str | None) -> list[str]:
        """`<server>  <uri>  <name>  <mime>` lines."""
        if server is not None and server not in self.connections:
            raise ToolError("not_found", f"no MCP server '{server}'")
        lines: list[str] = []
        for name in [server] if server else list(self.connections):
            result = await self.request(name, lambda s: s.list_resources())
            for resource in result.resources:
                lines.append(
                    f"{name}  {resource.uri}  {resource.name}  {resource.mime_type or '-'}"
                )
        return lines

    async def read_resource(self, ctx: Ctx, server: str, uri: str) -> ToolResult:
        """One resource as text, or as an image / a binary note."""
        try:
            result = await self.request(server, lambda s: s.read_resource(uri))
        except ToolError as err:
            if "unknown resource" in err.message.lower() or "not found" in err.message.lower():
                raise ToolError("not_found", f"no resource {uri} on {server}") from err
            raise
        parts: list[str] = []
        images: list[ImagePart] = []
        size = 0
        mime = "-"
        for content in result.contents:
            mime = content.mime_type or mime
            if isinstance(content, types.TextResourceContents):
                parts.append(content.text)
                size += len(content.text.encode("utf-8"))
            else:
                size += len(content.blob) * 3 // 4
                if mime.startswith("image/") and model_has_vision(ctx):
                    images.append(ImagePart(media_type=mime, data_b64=content.blob))
                else:
                    parts.append(
                        f"[binary resource, {size_text(len(content.blob) * 3 // 4)}, {mime}]"
                    )
        head = f"resource: {uri} ({mime}, {size_text(size)})"
        return ToolResult(call_id="", ok=True, text="\n".join([head, *parts]), images=images)

    async def close(self) -> None:
        """Disconnect every server."""
        for connection in self.connections.values():
            await connection.close()


def check_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> None:
    """The schema's required fields and basic JSON types (the server checks the rest)."""
    missing = [name for name in schema.get("required", []) if name not in arguments]
    if missing:
        raise ToolError("invalid_args", f"missing required arguments: {', '.join(missing)}")
    properties = schema.get("properties") or {}
    for name, value in arguments.items():
        wanted = (properties.get(name) or {}).get("type")
        allowed = JSON_TYPES.get(wanted) if isinstance(wanted, str) else None
        wrong_bool = isinstance(value, bool) and wanted in ("integer", "number")
        if allowed is not None and (not isinstance(value, allowed) or wrong_bool):
            raise ToolError("invalid_args", f"argument {name} must be of type {wanted}")


def convert(content: list[Any], vision: bool) -> tuple[str, list[ImagePart]]:
    """Content blocks -> (text, images)."""
    texts: list[str] = []
    images: list[ImagePart] = []
    for block in content:
        if isinstance(block, types.TextContent):
            texts.append(block.text)
        elif isinstance(block, types.ImageContent):
            if vision:
                images.append(ImagePart(media_type=block.mime_type, data_b64=block.data))
            else:
                texts.append(f"[image: {block.mime_type}, {size_text(len(block.data) * 3 // 4)}]")
        elif isinstance(block, types.ResourceLink):
            texts.append(f"resource: {block.uri}")
        elif isinstance(block, types.EmbeddedResource):
            inner = block.resource
            texts.append(
                inner.text
                if isinstance(inner, types.TextResourceContents)
                else f"resource: {inner.uri}"
            )
        else:
            texts.append(f"[{getattr(block, 'type', 'content')} block]")
    return "\n\n".join(texts), images


def first_sentence(text: str) -> str:
    """The first sentence (or line) of a description."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line.split(". ")[0].rstrip(".")[:120]
