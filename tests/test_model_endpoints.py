"""OpenAI-compatible endpoints: named servers whose names are providers.

An endpoint is stored with its API key under `models.endpoints`; the key is
write-only, so these tests check every way a catalog or setting leaves the
server. The Rust edge parses the same row — `tests/test_edge_supervisor.py`
diffs its `models` listing against this one.
"""

from __future__ import annotations

import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

ENDPOINT_FIELDS = "{ providers discoverableProviders endpoints { name baseUrl hasKey } available { id provider } }"
SECRET = "sk-very-secret"


def _context(session):
    from server.graphql.extensions import SESSION_LOCK_KEY

    return {"session": session, SESSION_LOCK_KEY: asyncio.Lock()}


async def _exec(query: str, variables: dict[str, Any] | None = None) -> Any:
    from db import async_session
    from server.graphql.schema import schema

    async with async_session() as s:
        return await schema.execute(query, variable_values=variables, context_value=_context(s))


async def _ok(query: str, variables: dict[str, Any] | None = None) -> dict[str, Any]:
    res = await _exec(query, variables)
    assert not res.errors, res.errors
    return res.data


async def _error(query: str, variables: dict[str, Any] | None = None) -> str:
    res = await _exec(query, variables)
    assert res.errors, res.data
    return res.errors[0].message


ADD = """mutation($name: String!, $url: String!, $key: String) {
  addEndpoint(name: $name, baseUrl: $url, apiKey: $key) %s }""" % ENDPOINT_FIELDS
UPDATE = """mutation($name: String!, $url: String!, $key: String, $clear: Boolean! = false) {
  updateEndpoint(name: $name, baseUrl: $url, apiKey: $key, clearKey: $clear) %s }""" % ENDPOINT_FIELDS
REMOVE = 'mutation($name: String!) { removeEndpoint(name: $name) %s }' % ENDPOINT_FIELDS


@pytest.fixture
def catalog_cache():
    """Restore the process-global catalog caches after each test."""
    from core import model_catalog

    custom, endpoints = model_catalog._custom_models, model_catalog._endpoints
    yield
    model_catalog.set_custom_models(custom)
    model_catalog._endpoints = endpoints


def test_parse_skips_what_cannot_be_used():
    from core.model_catalog import Endpoint, parse_endpoints

    rows = [
        {"name": "groq", "base_url": " https://api.groq.com/openai/v1/ ", "api_key": SECRET},
        {"name": "lmstudio", "base_url": "http://localhost:1234/v1", "api_key": ""},
        {"name": "groq", "base_url": "http://elsewhere"},  # repeated: first wins
        {"name": "ollama", "base_url": "http://x"},  # a built-in provider
        {"name": "Bad Name", "base_url": "http://x"},
        {"name": "-dash", "base_url": "http://x"},
        {"name": "x" * 33, "base_url": "http://x"},
        {"name": "nourl"},
        {"name": "blank", "base_url": "  "},
        {"name": 7, "base_url": "http://x"},
        "junk",
        None,
    ]
    assert parse_endpoints(rows) == [
        Endpoint("groq", "https://api.groq.com/openai/v1", SECRET),
        Endpoint("lmstudio", "http://localhost:1234/v1", None),
    ]


async def test_endpoint_lifecycle(database, catalog_cache):
    data = (await _ok(ADD, {"name": "groq", "url": "https://api.groq.com/openai/v1/", "key": SECRET}))["addEndpoint"]
    assert data["endpoints"] == [{"name": "groq", "baseUrl": "https://api.groq.com/openai/v1", "hasKey": True}]
    assert "groq" in data["providers"] and "groq" in data["discoverableProviders"]
    assert SECRET not in json.dumps(data)

    # Its name is now a provider a model id can carry.
    await _ok('mutation { addModel(id: "groq:llama-3.3-70b", label: "Llama") { default } }')
    from core.model_catalog import get_model_spec

    llm = get_model_spec("groq:llama-3.3-70b").build_llm()
    assert type(llm).__name__ == "ChatOpenAI"
    assert (llm.model_name, llm.openai_api_base) == ("llama-3.3-70b", "https://api.groq.com/openai/v1")
    assert llm.openai_api_key is not None and llm.openai_api_key.get_secret_value() == SECRET
    assert llm.stream_usage

    # No key sent back means keep it; clearKey drops it.
    data = (await _ok(UPDATE, {"name": "groq", "url": "https://api.groq.com/openai/v2"}))["updateEndpoint"]
    assert data["endpoints"][0] == {"name": "groq", "baseUrl": "https://api.groq.com/openai/v2", "hasKey": True}
    data = (await _ok(UPDATE, {"name": "groq", "url": "https://api.groq.com/openai/v2", "clear": True}))
    assert data["updateEndpoint"]["endpoints"][0]["hasKey"] is False
    assert get_model_spec("groq:llama-3.3-70b").build_llm().openai_api_key.get_secret_value() == "not-needed"

    # Not while a model uses it.
    assert "groq:llama-3.3-70b" in await _error(REMOVE, {"name": "groq"})
    await _ok('mutation { removeModel(id: "groq:llama-3.3-70b") { default } }')
    data = (await _ok(REMOVE, {"name": "groq"}))["removeEndpoint"]
    assert data["endpoints"] == [] and "groq" not in data["providers"]


async def test_bad_endpoints_are_refused(database, catalog_cache):
    assert "built-in provider" in await _error(ADD, {"name": "openrouter", "url": "https://x"})
    assert "Invalid endpoint name" in await _error(ADD, {"name": "My Server", "url": "https://x"})
    assert "http:// or https://" in await _error(ADD, {"name": "local", "url": "localhost:1234"})
    await _ok(ADD, {"name": "local", "url": "http://localhost:1234/v1"})
    assert "already exists" in await _error(ADD, {"name": "local", "url": "http://other"})
    assert "No endpoint" in await _error(UPDATE, {"name": "nobody", "url": "http://x"})
    assert "Unsupported provider" in await _error('mutation { addModel(id: "nobody:m", label: "M") { default } }')


async def test_settings_never_show_the_key(database, catalog_cache):
    await _ok(ADD, {"name": "groq", "url": "https://api.groq.com/openai/v1", "key": SECRET})
    listed = (await _ok("{ settings { key value managedBy } }"))["settings"]
    row = next(s for s in listed if s["key"] == "models.endpoints")
    assert row["managedBy"] == "Models"
    assert json.loads(row["value"]) == [{"name": "groq", "base_url": "https://api.groq.com/openai/v1", "api_key": "••••"}]
    one = (await _ok('{ setting(key: "models.endpoints") { value } }'))["setting"]
    assert SECRET not in one["value"]


class _ModelsServer:
    """A server's `GET /models`, recording the Authorization it was sent."""

    def __init__(self) -> None:
        self.auth: list[str | None] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                outer.auth.append(self.headers.get("authorization"))
                body = {"object": "list", "data": [
                    {"id": "qwen3-32b", "object": "model", "max_model_len": 32768},
                    {"id": "llama-3.3-70b", "object": "model", "context_length": 131072},
                    {"id": "whisper-large-v3", "object": "model"},
                    {"id": "kokoro-tts", "object": "model"},
                ]}
                self.send_response(200 if self.path == "/v1/models" else 404)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(body).encode())

            def log_message(self, format, *args):  # noqa: A002
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()


async def test_sync_lists_an_endpoints_models(database, catalog_cache):
    srv = _ModelsServer()
    try:
        await _ok(ADD, {"name": "local", "url": srv.url, "key": SECRET})
        data = await _ok(
            'query { modelSync(provider: "local") { provider skipped newModels { id contextWindow likelyChat } } }'
        )
    finally:
        srv.server.shutdown()
    [report] = data["modelSync"]
    assert report["skipped"] is None
    assert sorted(report["newModels"], key=lambda m: m["id"]) == sorted([
        {"id": "local:qwen3-32b", "contextWindow": 32768, "likelyChat": True},
        {"id": "local:llama-3.3-70b", "contextWindow": 131072, "likelyChat": True},
        {"id": "local:whisper-large-v3", "contextWindow": None, "likelyChat": True},
        {"id": "local:kokoro-tts", "contextWindow": None, "likelyChat": False},
    ], key=lambda m: m["id"])
    assert srv.auth == [f"Bearer {SECRET}"]
