"""A tiny MCP server with one tool, word_count; Forge offers it as mcp__demo__word_count."""

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("demo")


READ_ONLY = ToolAnnotations(readOnlyHint=True)


@server.tool(description="Count the words in a text.", annotations=READ_ONLY)
def word_count(text: str) -> str:
    """How many words the text has."""
    return f"{len(text.split())} words"


if __name__ == "__main__":
    server.run("stdio")
