"""OpenAI-compatible endpoints: named servers whose names are providers.

An endpoint is stored with its API key under `models.endpoints`; the key is
write-only. The Rust server parses the same row (`edge/src/catalog.rs`);
`tests/test_edge_serving.py` diffs its `models` listing against Python's.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


SECRET = "sk-very-secret"


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
