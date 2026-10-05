"""The echo server over a network transport, for tests/test_edge_mcp.py.

    python http_mcp_server.py <streamable-http|streamable-http-json|sse|websocket> <port>

Streamable HTTP answers with event streams (FastMCP's default) or, with
`-json`, plain JSON bodies; websocket is the `mcp` subprotocol at `/ws`.
"""

import sys

from mcp.server.fastmcp import FastMCP

transport, port = sys.argv[1], int(sys.argv[2])
mcp = FastMCP("web", host="127.0.0.1", port=port, json_response=transport.endswith("-json"))


@mcp.tool()
def echo(text: str) -> str:
    """Return the text you were given."""
    return f"echo: {text}"


@mcp.tool()
def explode() -> str:
    """Always fails."""
    raise ValueError("boom over the wire")


def _websocket() -> None:
    import uvicorn
    from mcp.server.websocket import websocket_server

    server = mcp._mcp_server

    async def app(scope, receive, send):
        if scope["type"] != "websocket":
            return
        async with websocket_server(scope, receive, send) as (read, write):
            await server.run(read, write, server.create_initialization_options())

    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    if transport == "websocket":
        _websocket()
    else:
        mcp.run(transport="sse" if transport == "sse" else "streamable-http")
