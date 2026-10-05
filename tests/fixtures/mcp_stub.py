"""A small MCP server over stdio for tests (run: python mcp_stub.py [extra_tool_count])."""

import base64
import sys

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("stub")

# 1x1 transparent PNG
PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


@server.tool(description="Add two numbers and return the sum.")
def add(a: int, b: int) -> str:
    return str(a + b)


@server.tool(description="Look up a user by name.", annotations=ToolAnnotations(readOnlyHint=True))
def lookup(name: str) -> str:
    return f"user {name}: active"


@server.tool(description="Always fails.")
def explode() -> str:
    raise RuntimeError("the reactor is offline")


@server.tool(description="Return a tiny image.")
def picture() -> Image:
    return Image(data=PIXEL, format="png")


@server.resource("memo://welcome", name="welcome", mime_type="text/plain")
def welcome() -> str:
    return "Welcome to the stub server."


def add_many(count: int) -> None:
    """Register `count` extra tools for deferred-loading tests."""
    for n in range(count):
        topic = ["invoice", "weather", "calendar", "ticket", "deploy"][n % 5]

        server.tool(name=f"{topic}_action_{n}", description=f"Handle {topic} request number {n}.")(
            make_handler(n)
        )


def make_handler(number: int):  # type: ignore[no-untyped-def]
    """A tool body that remembers its number."""

    def handler(text: str = "") -> str:
        return f"tool {number} got {text}"

    return handler


if __name__ == "__main__":
    add_many(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    server.run("stdio")
