"""A minimal stdio MCP server used by tests/test_mcp_integration.py.

Deliberately real: it exercises the langchain-mcp-adapters contract we depend
on (per-server tool loading, ToolCall invocation, isError handling) rather than
a stand-in that could agree with a wrong assumption.
"""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("echo")


@mcp.tool()
def echo(text: str) -> str:
    """Return the text you were given."""
    return f"echo: {text}"


@mcp.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@mcp.tool()
def explode() -> str:
    """Always fails — exercises the MCP error path."""
    raise ValueError("boom from the server")


if __name__ == "__main__":
    mcp.run()
