"""An MCP server behind OAuth, for tests/test_edge_cli_mcp.py.

    python oauth_mcp_server.py <port>

One app is both the MCP server (`/mcp`, Streamable HTTP, a bearer token
required) and its authorization server: protected-resource and
authorization-server metadata, dynamic client registration, `/authorize`
(which approves at once, as a person clicking Allow would), and `/token`
(codes checked against PKCE, refresh tokens rotated).

Env: `TOKEN_TTL` (seconds an access token lives, default 3600), `NO_HINT=1`
(the 401 doesn't name the metadata, so the client looks in the well-known
place), `NO_REGISTRATION=1` (no dynamic registration; client `fixed` is
registered for any redirect). `POST /revoke-all` drops every access token;
`GET /log` lists what the token endpoint was asked.
"""

import base64
import hashlib
import os
import secrets
import sys
import time
from urllib.parse import parse_qs, urlencode

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Route

port = int(sys.argv[1])
BASE = f"http://127.0.0.1:{port}"
RESOURCE = f"{BASE}/mcp"
TTL = int(os.environ.get("TOKEN_TTL", "3600"))

mcp = FastMCP("guarded", host="127.0.0.1", port=port, stateless_http=True, json_response=True)


@mcp.tool()
def whoami() -> str:
    """Who the token belongs to."""
    return "signed in"


clients: dict[str, list[str]] = {"fixed": []} if os.environ.get("NO_REGISTRATION") else {}
codes: dict[str, tuple[str, str, str]] = {}
access: dict[str, float] = {}
refresh: set[str] = set()
log: list[str] = []


async def resource_metadata(_: Request) -> JSONResponse:
    return JSONResponse({"resource": RESOURCE, "authorization_servers": [BASE], "scopes_supported": ["mcp"]})


async def server_metadata(_: Request) -> JSONResponse:
    meta = {
        "issuer": BASE, "authorization_endpoint": f"{BASE}/authorize", "token_endpoint": f"{BASE}/token",
        "response_types_supported": ["code"], "code_challenge_methods_supported": ["S256"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
    }
    if not os.environ.get("NO_REGISTRATION"):
        meta["registration_endpoint"] = f"{BASE}/register"
    return JSONResponse(meta)


async def register(request: Request) -> JSONResponse:
    body = await request.json()
    client_id = secrets.token_hex(8)
    clients[client_id] = body["redirect_uris"]
    return JSONResponse({**body, "client_id": client_id}, status_code=201)


def _refuse(why: str) -> JSONResponse:
    return JSONResponse({"error": "invalid_request", "error_description": why}, status_code=400)


async def authorize(request: Request):
    q = request.query_params
    if q.get("client_id") not in clients:
        return _refuse("unknown client")
    if clients[q["client_id"]] and q.get("redirect_uri") not in clients[q["client_id"]]:
        return _refuse("unregistered redirect_uri")
    if q.get("code_challenge_method") != "S256" or not q.get("code_challenge"):
        return _refuse("PKCE S256 required")
    if q.get("resource") != RESOURCE or q.get("scope") != "mcp":
        return _refuse(f"wrong resource or scope: {q.get('resource')} {q.get('scope')}")
    code = secrets.token_hex(8)
    codes[code] = (q["client_id"], q["code_challenge"], q["redirect_uri"])
    return RedirectResponse(f"{q['redirect_uri']}?{urlencode({'code': code, 'state': q['state']})}", status_code=302)


def _issue() -> JSONResponse:
    a, r = secrets.token_hex(8), secrets.token_hex(8)
    access[a] = time.time() + TTL
    refresh.add(r)
    return JSONResponse({"access_token": a, "token_type": "Bearer", "expires_in": TTL, "refresh_token": r, "scope": "mcp"})


async def token(request: Request) -> JSONResponse:
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    log.append(form.get("grant_type", "?"))
    if form.get("resource") != RESOURCE:
        return _refuse("wrong resource")
    if form.get("grant_type") == "authorization_code":
        client_id, challenge, redirect = codes.pop(form.get("code", ""), ("", "", ""))
        digest = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest())
        if (digest.rstrip(b"=").decode() != challenge or form.get("client_id") != client_id
                or form.get("redirect_uri") != redirect):
            return JSONResponse({"error": "invalid_grant"}, status_code=400)
        return _issue()
    if form.get("grant_type") == "refresh_token" and form.get("refresh_token") in refresh:
        refresh.discard(form["refresh_token"])  # rotated: each one works once
        return _issue()
    return JSONResponse({"error": "invalid_grant", "error_description": "unknown refresh token"}, status_code=400)


async def revoke_all(_: Request) -> JSONResponse:
    access.clear()
    return JSONResponse({})


async def show_log(_: Request) -> JSONResponse:
    return JSONResponse(log)


oauth = Starlette(routes=[
    Route("/.well-known/oauth-protected-resource/mcp", resource_metadata),
    Route("/.well-known/oauth-authorization-server", server_metadata),
    Route("/register", register, methods=["POST"]),
    Route("/authorize", authorize),
    Route("/token", token, methods=["POST"]),
    Route("/revoke-all", revoke_all, methods=["POST"]),
    Route("/log", show_log),
])
inner = mcp.streamable_http_app()


async def app(scope, receive, send):
    if scope["type"] == "http" and not scope["path"].startswith("/mcp"):
        return await oauth(scope, receive, send)
    if scope["type"] == "http":
        auth = dict(scope["headers"]).get(b"authorization", b"").decode()
        if access.get(auth.removeprefix("Bearer "), 0) < time.time():
            hint = "" if os.environ.get("NO_HINT") else \
                f', resource_metadata="{BASE}/.well-known/oauth-protected-resource/mcp"'
            refused = JSONResponse({"error": "invalid_token"}, status_code=401,
                                   headers={"WWW-Authenticate": f'Bearer error="invalid_token", scope="mcp"{hint}'})
            return await refused(scope, receive, send)
    await inner(scope, receive, send)


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
