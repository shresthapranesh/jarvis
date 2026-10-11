"""A stdio MCP server whose tool takes a while, for checking that the edge
runs a batch's independent calls at the same time."""

import time

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("slow")


@mcp.tool()
def wait(label: str, seconds: float) -> str:
    """Sleep, then say when it started and ended."""
    start = time.time()
    time.sleep(seconds)
    return f"{label} {start:.3f} {time.time():.3f}"


if __name__ == "__main__":
    mcp.run()
