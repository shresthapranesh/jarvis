"""`modelSync` in the edge (`edge/src/discovery.rs`, `aws.rs`,
`gql/model_sync.rs`) against Python's (`core/model_discovery.py`).

One fake server plays every provider — Google, Anthropic, Bedrock (checking
each SigV4 signature with botocore's own signer), Ollama, OpenRouter, the
operator's endpoints, and the EC2 metadata service. The edge's answers are
diffed against Python's, recorded while it existed (`python_golden.py`).

Skipped when `cargo` isn't installed.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import pytest

from edge_support import _relay_text, _run_edge, edge_binary  # noqa: F401 — edge_binary is a fixture
from python_golden import recorded

SECRET = "fake-secret"
IMDS_SECRET = "imds-secret"

SYNC = """query($provider: String, $probe: Boolean!) {
  modelSync(provider: $provider, probe: $probe) {
    provider offered skipped probed clean missing
    unreachable { id reason }
    windows { id label provider catalogWindow providerWindow builtin }
    newModels { id label provider contextWindow description likelyChat }
  }
}"""

# Every variable either side reads to find a provider or its credentials.
_PROVIDER_ENV = (
    "GOOGLE_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY", "OPENROUTER_API_KEY", "META_API_KEY",
    "JARVIS_GOOGLE_BASE_URL", "GOOGLE_GEMINI_BASE_URL", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL",
    "JARVIS_OPENROUTER_BASE_URL", "OLLAMA_HOST",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_SECURITY_TOKEN", "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE", "AWS_REGION", "AWS_DEFAULT_REGION", "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_BEDROCK",
    "AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
    "AWS_EC2_METADATA_DISABLED", "AWS_EC2_METADATA_SERVICE_ENDPOINT", "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI", "AWS_CONTAINER_CREDENTIALS_FULL_URI",
)


def _ok(body: Any) -> tuple[int, dict[str, str], bytes]:
    return 200, {"content-type": "application/json"}, json.dumps(body).encode()


def _err(status: int, body: Any, headers: dict[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    return status, {"content-type": "application/json", **(headers or {})}, raw


GOOGLE_PAGES = {
    None: {
        "models": [
            {"name": "models/gemini-2.5-pro", "displayName": "Gemini 2.5 Pro", "inputTokenLimit": 1048576,
             "supportedGenerationMethods": ["generateContent", "countTokens"], "description": "pro"},
            {"name": "models/gemini-2.0-flash", "displayName": "Gemini 2.0 Flash", "inputTokenLimit": 999999,
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemma-4-31b-it", "inputTokenLimit": 131072, "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
        ],
        "nextPageToken": "p2",
    },
    "p2": {
        "models": [
            {"name": "models/gemini-2.5-flash-preview-tts", "displayName": "TTS",
             "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-9", "displayName": "", "description": "new",
             "supportedGenerationMethods": ["generateContent"]},
        ],
    },
}

ANTHROPIC_PAGES = {
    None: {"data": [{"id": "claude-opus-4-7", "display_name": "Claude Opus 4.7"}, {"id": "claude-new", "display_name": None}],
           "has_more": True, "first_id": "claude-opus-4-7", "last_id": "claude-new"},
    "claude-new": {"data": [{"id": "claude-haiku-4-5-20251001", "display_name": "Haiku"}], "has_more": False,
                   "first_id": "claude-haiku-4-5-20251001", "last_id": "claude-haiku-4-5-20251001"},
}

BEDROCK = {"modelSummaries": [
    {"modelId": "us.anthropic.claude-sonnet-4-6", "modelName": "Claude Sonnet 4.6", "providerName": "Anthropic",
     "inferenceTypesSupported": ["ON_DEMAND"]},
    {"modelId": "amazon.nova-pro-v1:0", "modelName": "Nova Pro", "providerName": "Amazon",
     "inferenceTypesSupported": ["ON_DEMAND", "PROVISIONED"]},
    {"modelId": "x.provisioned", "inferenceTypesSupported": ["PROVISIONED"]},
    {"modelId": "meta.llama", "inferenceTypesSupported": ["ON_DEMAND"]},
]}

OLLAMA = {"models": [{"name": "llama3.3"}, {"name": "mistral:7b"}, {"name": ""}, {"model": "nameless"}]}

OPENROUTER = {"data": [
    {"id": "anthropic/claude-x", "name": "Claude X", "context_length": 200000,
     "architecture": {"output_modalities": ["text"]}, "description": "  desc \n"},
    {"id": "google/img", "architecture": {"output_modalities": ["image"]}, "top_provider": {"context_length": 32768.0}},
    {"id": "free/model:free", "context_length": 0, "top_provider": {"context_length": 8192}, "description": ""},
    {"id": ""},
    {"id": "x/veo-3", "architecture": {}},
]}

ENDPOINT = {"object": "list", "data": [
    {"id": "qwen-7b", "max_model_len": 32768}, {"id": "tts-1"}, {"id": 5}, "junk",
    {"id": "m-ctx", "context_window": True, "context_length": 4096},
]}

# A probe of one of these models fails, the way each provider says so.
FAILS = {"gemini-2.0-flash", "qwen3:32b", "claude-sonnet-4-6", "anthropic.claude-3-haiku-20240307-v1:0", "qwen-7b",
         "gone/model"}


class Fake:
    def __init__(self) -> None:
        self.signatures: list[bool] = []
        self.overrides: dict[str, tuple[int, dict[str, str], bytes]] = {}
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self, method: str) -> None:
                body = self.rfile.read(int(self.headers.get("content-length") or 0))
                status, headers, raw = fake.route(method, self.path, dict(self.headers), body)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("content-length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):  # noqa: N802 — http.server's spelling
                self._answer("GET")

            def do_POST(self):  # noqa: N802
                self._answer("POST")

            def do_PUT(self):  # noqa: N802
                self._answer("PUT")

            def log_message(self, format, *args):  # noqa: A002 — the parent's name
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()

    def _signed(self, method: str, path: str, headers: dict[str, str], body: bytes) -> bool:
        """botocore's own signer over what arrived, at the time it was signed."""
        from botocore.auth import SigV4Auth
        from botocore.awsrequest import AWSRequest
        from botocore.credentials import Credentials

        h = {k.lower(): v for k, v in headers.items()}
        m = re.fullmatch(
            r"AWS4-HMAC-SHA256 Credential=([^/]+)/(\d{8})/([^/]+)/([^/]+)/aws4_request, "
            r"SignedHeaders=([^,]+), Signature=([0-9a-f]{64})", h.get("authorization", ""),
        )
        if not m:
            return False
        secret = IMDS_SECRET if "x-amz-security-token" in h else SECRET
        req = AWSRequest(method=method, url=self.url + path, data=body, headers={n: h[n] for n in m[5].split(";")})
        req.context["timestamp"] = h["x-amz-date"]
        signer = SigV4Auth(Credentials(m[1], secret, h.get("x-amz-security-token")), m[4], m[3])
        sts = signer.string_to_sign(req, signer.canonical_request(req))
        return signer.signature(sts, req) == m[6]

    def route(self, method: str, raw_path: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
        parts = urlsplit(raw_path)
        path, query = parts.path, {k: v[0] for k, v in parse_qs(parts.query).items()}
        for prefix, answer in self.overrides.items():
            if path.startswith(prefix):
                return answer
        h = {k.lower(): v for k, v in headers.items()}
        sent = json.loads(body) if body else {}

        # The EC2 metadata service.
        if path == "/imds/latest/api/token" and method == "PUT":
            return 200, {}, b"imds-token"
        if path.startswith("/imds/latest/meta-data/iam/security-credentials/"):
            if h.get("x-aws-ec2-metadata-token") != "imds-token":
                return 401, {}, b""
            if path.endswith("/"):
                return 200, {}, b"jarvis-role"
            return _ok({"Code": "Success", "AccessKeyId": "IMDSKEY", "SecretAccessKey": IMDS_SECRET, "Token": "imds-session",
                        "Expiration": "2099-01-01T00:00:00Z"})

        if path.startswith(("/bedrock/", "/bedrock-runtime/")):
            ok = self._signed(method, raw_path, headers, body)
            self.signatures.append(ok)
            if not ok:
                return _err(403, {"message": "The request signature we calculated does not match"},
                            {"x-amzn-errortype": "InvalidSignatureException:http://internal.amazon.com/"})
        if path == "/bedrock/foundation-models":
            assert query == {"byOutputModality": "TEXT"}
            return _ok(BEDROCK)
        if m := re.fullmatch(r"/bedrock-runtime/model/([^/]+)/converse", path):
            if unquote(m[1]) in FAILS:
                return _err(400, {"message": "The provided model identifier is invalid."},
                            {"x-amzn-errortype": "ValidationException:http://internal.amazon.com/"})
            return _ok({"output": {"message": {"role": "assistant", "content": [{"text": "h"}]}}, "stopReason": "max_tokens",
                        "usage": {"inputTokens": 1, "outputTokens": 1, "totalTokens": 2}, "metrics": {"latencyMs": 1}})

        if path == "/google/v1beta/models":
            if h.get("x-goog-api-key") != "g-key":
                return _err(403, {"error": {"code": 403, "message": "bad key", "status": "PERMISSION_DENIED"}})
            assert query.get("pageSize") == "1000"
            return _ok(GOOGLE_PAGES[query.get("pageToken")])
        if m := re.fullmatch(r"/google/v1beta/models/(.+):generateContent", path):
            if m[1] in FAILS:
                return _err(404, {"error": {"code": 404, "message": f"models/{m[1]} is not found", "status": "NOT_FOUND"}})
            return _ok({"candidates": [{"content": {"role": "model", "parts": [{"text": "h"}]}, "finishReason": "MAX_TOKENS",
                                        "index": 0}],
                        "usageMetadata": {"promptTokenCount": 1, "candidatesTokenCount": 1, "totalTokenCount": 2},
                        "modelVersion": m[1]})

        if path == "/anthropic/v1/models":
            assert h.get("x-api-key") == "a-key" and h.get("anthropic-version") == "2023-06-01"
            return _ok(ANTHROPIC_PAGES[query.get("after_id")])
        if path == "/anthropic/v1/messages":
            if sent["model"] in FAILS:
                return _err(404, {"type": "error", "error": {"type": "not_found_error", "message": f"model: {sent['model']}"}})
            return _ok({"id": "msg_1", "type": "message", "role": "assistant", "model": sent["model"],
                        "content": [{"type": "text", "text": "h"}], "stop_reason": "max_tokens", "stop_sequence": None,
                        "usage": {"input_tokens": 1, "output_tokens": 1}})

        if path == "/ollama/api/tags":
            return _ok(OLLAMA)
        if path == "/ollama/api/chat":
            if sent["model"] in FAILS:
                return _err(404, {"error": f"model '{sent['model']}' not found"})
            return _ok({"model": sent["model"], "created_at": "2026-01-01T00:00:00Z",
                        "message": {"role": "assistant", "content": "h"}, "done": True, "done_reason": "length"})

        if path in ("/openrouter/models", "/ep/models"):
            if path == "/ep/models":
                assert h.get("authorization") == "Bearer ep-key"
            return _ok(OPENROUTER if path.startswith("/openrouter") else ENDPOINT)
        if path in ("/openrouter/chat/completions", "/ep/chat/completions"):
            if sent["model"] in FAILS:
                return _err(404, {"error": {"message": f"The model `{sent['model']}` does not exist",
                                            "type": "invalid_request_error", "code": "model_not_found"}})
            return _ok({"id": "c1", "object": "chat.completion", "created": 0, "model": sent["model"],
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "h"}, "finish_reason": "length"}],
                        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})

        if path == "/moved/models":
            return 301, {"location": "https://elsewhere.example/v1/models"}, b""
        return 404, {"content-type": "text/plain"}, b"no such route"


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.close()


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
async def catalog(database):
    """Custom models and endpoints both sides read, and Python's caches put
    back afterwards."""
    from core import model_catalog
    from db import async_session
    from db.ops import set_setting

    saved = model_catalog._custom_models, model_catalog._endpoints
    async with async_session() as s:
        await set_setting(s, "models.custom", json.dumps([
            {"id": "openrouter:anthropic/claude-x", "label": "Claude X (mine)", "provider": "openrouter", "context_window": 100000},
            {"id": "openrouter:gone/model", "label": "Gone"},
            {"id": "local:qwen-7b", "label": "Qwen"},
            {"id": "bedrock:anthropic.claude-3-haiku-20240307-v1:0", "label": "Haiku 3"},
        ]))
        await s.commit()
    yield
    model_catalog._custom_models, model_catalog._endpoints = saved


async def _endpoints(fake: Fake) -> None:
    from db import async_session
    from db.ops import set_setting

    async with async_session() as s:
        await set_setting(s, "models.endpoints", json.dumps([
            {"name": "local", "base_url": f"{fake.url}/ep/", "api_key": "ep-key"},
            {"name": "missing", "base_url": f"{fake.url}/missing"},
            {"name": "moved", "base_url": f"{fake.url}/moved"},
            {"name": "down", "base_url": f"http://127.0.0.1:{_closed_port()}/v1"},
        ]))
        await s.commit()


def _env(monkeypatch, tmp_path: Path, values: dict[str, str]) -> dict[str, str]:
    """Both sides' environment: nothing a provider reads but `values`."""
    import boto3

    # boto3's default session keeps the credentials it first resolved.
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    for var in _PROVIDER_ENV:
        monkeypatch.delenv(var, raising=False)
    env = {
        "AWS_CONFIG_FILE": str(tmp_path / "aws-config"),
        "AWS_SHARED_CREDENTIALS_FILE": str(tmp_path / "aws-credentials"),
        "AWS_EC2_METADATA_DISABLED": "true",
        **values,
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    # Python's OpenRouter client reads the constant, not the variable.
    if "JARVIS_OPENROUTER_BASE_URL" in env:
        from core import model_catalog

        monkeypatch.setattr(model_catalog, "OPENROUTER_BASE_URL", env["JARVIS_OPENROUTER_BASE_URL"])
    return env


def _providers(fake: Fake) -> dict[str, str]:
    return {
        "JARVIS_GOOGLE_BASE_URL": f"{fake.url}/google", "GOOGLE_GEMINI_BASE_URL": f"{fake.url}/google",
        "GOOGLE_API_KEY": "g-key",
        "ANTHROPIC_BASE_URL": f"{fake.url}/anthropic", "ANTHROPIC_API_KEY": "a-key",
        "AWS_ACCESS_KEY_ID": "AKIDFAKE", "AWS_SECRET_ACCESS_KEY": SECRET, "AWS_REGION": "us-west-2",
        "AWS_ENDPOINT_URL_BEDROCK": f"{fake.url}/bedrock", "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": f"{fake.url}/bedrock-runtime",
        "OLLAMA_HOST": f"{fake.url}/ollama",
        "JARVIS_OPENROUTER_BASE_URL": f"{fake.url}/openrouter", "OPENROUTER_API_KEY": "or-key",
    }


async def _python(query: str, variables: dict[str, Any]) -> dict[str, Any]:
    from db import async_session
    from server.graphql.extensions import SESSION_LOCK_KEY
    from server.graphql.schema import schema

    async with async_session() as s:
        res = await schema.execute(
            query, variable_values=variables,
            context_value={"session": s, SESSION_LOCK_KEY: asyncio.Lock(), "caller": "human"},
        )
    out: dict[str, Any] = {"data": res.data}
    if res.errors:
        out["errors"] = [{"message": e.message, "path": e.path} for e in res.errors]
    return out


async def _edge(client, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post("/graphql", json={"query": query, "variables": variables}, timeout=60)
    assert resp.status_code == 200
    body = resp.json()
    out: dict[str, Any] = {"data": body.get("data")}
    if body.get("errors"):
        out["errors"] = [{"message": e["message"], "path": e.get("path")} for e in body["errors"]]
    return out


_PORT = re.compile(r"(127\.0\.0\.1|localhost):\d+")


def _unport(value: Any) -> Any:
    """The fake provider's and the closed ports differ from run to run."""
    return json.loads(_PORT.sub(r"\1:<port>", json.dumps(value)))


async def _both(client, variables: dict[str, Any], query: str = SYNC) -> dict[str, Any]:
    """The edge's answer, diffed against Python's recorded one."""
    python = await recorded(lambda: _unport_async(_python(query, variables)))
    edge = _unport(await _edge(client, query, variables))
    if edge != python and (edge["data"] and python["data"]):
        for e, p in zip(edge["data"]["modelSync"], python["data"]["modelSync"], strict=True):
            assert e == p, variables
    assert edge == python, variables
    return python


async def _unport_async(answer: Any) -> Any:
    return _unport(await answer)


def _reports(answer: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["provider"]: r for r in answer["data"]["modelSync"]}


async def test_listings_match_python(catalog, fake, edge_binary, work_dir, tmp_path, monkeypatch):
    await _endpoints(fake)
    env = _env(monkeypatch, tmp_path, _providers(fake))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        answer = await _both(client, {"probe": False})
        # The settings page's own query, aliases and all.
        await _both(client, {"provider": None, "probe": False}, _relay_text("ModelSyncQuery"))
        for provider in ("google_genai", "local", "bedrock"):
            await _both(client, {"provider": provider, "probe": False})
        for bad in ("meta", "nope", ""):
            assert "errors" in await _both(client, {"provider": bad, "probe": False})

    reports = _reports(answer)
    assert list(reports) == sorted(reports)
    assert reports["google_genai"]["missing"] == ["google_genai:gemini-3.1-flash-lite", "google_genai:gemma-4-26b-a4b-it"]
    assert {w["id"]: w["catalogWindow"] for w in reports["google_genai"]["windows"]} == {
        "google_genai:gemma-4-31b-it": None, "google_genai:gemini-2.0-flash": 1048576,
    }
    assert [m["likelyChat"] for m in reports["google_genai"]["newModels"]] == [False, True]
    assert reports["anthropic"]["offered"] == 3  # both pages
    assert {m["id"] for m in reports["bedrock"]["newModels"]} == {"bedrock:amazon.nova-pro-v1:0", "bedrock:meta.llama"}
    assert all(fake.signatures) and len(fake.signatures) >= 2
    assert reports["local"]["windows"] == [{"id": "local:qwen-7b", "label": "Qwen", "provider": "local",
                                            "catalogWindow": None, "providerWindow": 32768, "builtin": False}]
    assert reports["down"]["skipped"].endswith("[Errno 61] Connection refused") or "Connection refused" in reports["down"]["skipped"]
    assert reports["missing"]["skipped"].startswith("could not list missing's models at ")
    assert "Redirect location: 'https://elsewhere.example/v1/models'" in reports["moved"]["skipped"]


async def test_failures_match_python(catalog, fake, edge_binary, work_dir, tmp_path, monkeypatch):
    """No keys, a profile that isn't there, providers that refuse."""
    env = _env(monkeypatch, tmp_path, {
        "OLLAMA_HOST": f"127.0.0.1:{_closed_port()}",
        "JARVIS_OPENROUTER_BASE_URL": f"{fake.url}/openrouter-down",
        "AWS_PROFILE": "nope",
    })
    fake.overrides["/openrouter-down"] = (503, {"content-type": "text/plain"}, b"busy")
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        reports = _reports(await _both(client, {"probe": True}))
    assert reports["google_genai"]["skipped"] == "GOOGLE_API_KEY is not set"
    assert reports["anthropic"]["skipped"] == "ANTHROPIC_API_KEY is not set"
    assert reports["bedrock"]["skipped"] == "ListFoundationModels failed (us-east-1): The config profile (nope) could not be found"
    assert reports["ollama"]["skipped"].startswith("could not reach ollama at http://127.0.0.1:")
    assert reports["openrouter"]["skipped"].startswith("could not reach OpenRouter: Server error '503 Service Unavailable'")


async def test_refusals_match_python(catalog, fake, edge_binary, work_dir, tmp_path, monkeypatch):
    env = _env(monkeypatch, tmp_path, {
        **_providers(fake),
        "GOOGLE_API_KEY": "wrong",
        "OPENROUTER_API_KEY": "",
    })
    del env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"]
    monkeypatch.delenv("AWS_ACCESS_KEY_ID")
    monkeypatch.delenv("AWS_SECRET_ACCESS_KEY")
    fake.overrides["/anthropic/v1/models"] = _err(
        401, {"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})
    fake.overrides["/ollama/api/tags"] = _err(500, b"<html>oops</html>")
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        # OpenRouter's listing is public; its probes need the key it lacks.
        reports = _reports(await _both(client, {"probe": True}))
    assert reports["google_genai"]["skipped"].startswith("ListModels failed (403): ")
    assert reports["anthropic"]["skipped"] == (
        "models.list failed: Error code: 401 - {'type': 'error', 'error': {'type': 'authentication_error', "
        "'message': 'invalid x-api-key'}}"
    )
    assert reports["bedrock"]["skipped"] == "ListFoundationModels failed (us-west-2): Unable to locate credentials"
    assert reports["openrouter"]["unreachable"][0]["reason"] == (
        "OPENROUTER_API_KEY is not set (required for 'openrouter:anthropic/claude-x')"
    )


async def test_an_instance_role_signs_like_boto3(catalog, fake, edge_binary, work_dir, tmp_path, monkeypatch):
    values = {k: v for k, v in _providers(fake).items() if not k.startswith("AWS_ACCESS") and not k.startswith("AWS_SECRET")}
    env = _env(monkeypatch, tmp_path, {**values, "AWS_EC2_METADATA_DISABLED": "false",
                                       "AWS_EC2_METADATA_SERVICE_ENDPOINT": f"{fake.url}/imds/"})
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        report = (await _both(client, {"provider": "bedrock", "probe": True}))["data"]["modelSync"][0]
    assert report["skipped"] is None and report["offered"] == 3
    assert fake.signatures and all(fake.signatures)


async def test_probes_match_python(catalog, fake, edge_binary, work_dir, tmp_path, monkeypatch):
    await _endpoints(fake)
    env = _env(monkeypatch, tmp_path, _providers(fake))
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        unreachable = {}
        for provider in ("google_genai", "anthropic", "bedrock", "ollama", "openrouter", "local"):
            report = (await _both(client, {"provider": provider, "probe": True}))["data"]["modelSync"][0]
            assert report["probed"]
            unreachable.update({u["id"]: u["reason"] for u in report["unreachable"]})
    assert set(unreachable) == {
        "google_genai:gemini-2.0-flash", "anthropic:claude-sonnet-4-6", "bedrock:anthropic.claude-3-haiku-20240307-v1:0",
        "ollama:qwen3:32b", "openrouter:gone/model", "local:qwen-7b",
    }
    assert all(fake.signatures)


async def test_a_credential_source_the_edge_does_not_read_is_skipped(catalog, fake, edge_binary, work_dir, tmp_path,
                                                                      monkeypatch):
    """SSO, assume-role and the other sources only boto3 read: Bedrock is
    skipped, and the report says why."""
    (tmp_path / "aws-config").write_text("[profile sso]\nsso_start_url = https://example.awsapps.com/start\n")
    env = _env(monkeypatch, tmp_path, {"AWS_PROFILE": "sso"})
    async with _run_edge(edge_binary, work_dir, work_dir / "database.db", env) as client:
        answer = await _edge(client, SYNC, {"provider": "bedrock", "probe": False})
    [report] = answer["data"]["modelSync"]
    assert report["skipped"].startswith("AWS credentials: ")
