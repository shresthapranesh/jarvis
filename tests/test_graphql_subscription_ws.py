"""The GraphQL subscription WebSocket must accept a connection.

`get_context` is resolved by FastAPI's dependency solver for BOTH the HTTP
route and the subscription WebSocket. A `Request`-typed parameter is only
filled in for HTTP scopes, so declaring one makes every WS connection die with
`get_context() missing 1 required positional argument` -- queries and mutations
keep working while the UI silently loses all live streaming (no tokens, no
steps; the finished message only appears after a reload). Regression test for
that failure mode: connect and complete the graphql-transport-ws handshake.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from strawberry.subscriptions import GRAPHQL_TRANSPORT_WS_PROTOCOL, GRAPHQL_WS_PROTOCOL


def _client(work_dir: Path) -> TestClient:
    from server.graphql.router import router

    app = FastAPI()
    app.include_router(router, prefix="/graphql")
    return TestClient(app)


def test_ws_handshake_succeeds(work_dir: Path) -> None:
    with _client(work_dir).websocket_connect("/graphql", [GRAPHQL_TRANSPORT_WS_PROTOCOL]) as ws:
        ws.send_json({"type": "connection_init"})
        assert ws.receive_json()["type"] == "connection_ack"


def test_ws_handshake_succeeds_legacy_protocol(work_dir: Path) -> None:
    with _client(work_dir).websocket_connect("/graphql", [GRAPHQL_WS_PROTOCOL]) as ws:
        ws.send_json({"type": "connection_init"})
        assert ws.receive_json()["type"] == "connection_ack"


def test_http_still_sees_the_caller_header(work_dir: Path) -> None:
    """The header the SDK sets must survive the HTTPConnection switch."""
    import asyncio

    from starlette.requests import HTTPConnection

    from server.graphql.context import get_context

    def _scope(headers: list[tuple[bytes, bytes]]) -> HTTPConnection:
        return HTTPConnection({"type": "http", "headers": headers})

    ctx = asyncio.run(
        get_context(
            _scope([(b"x-jarvis-caller", b"agent"), (b"x-jarvis-conversation", b"conv1")]),
            session=None,  # type: ignore[arg-type]
        )
    )
    assert ctx["caller"] == "agent"
    assert ctx["caller_conversation_id"] == "conv1"

    plain = asyncio.run(get_context(_scope([]), session=None))  # type: ignore[arg-type]
    assert plain["caller"] == "human"
    assert plain["caller_conversation_id"] is None
