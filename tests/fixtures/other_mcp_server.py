"""A second stdio MCP server, so attribution has something to get wrong."""

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("other")


@mcp.tool()
def ping() -> str:
    """Return pong."""
    return "pong"


if __name__ == "__main__":
    mcp.run()
