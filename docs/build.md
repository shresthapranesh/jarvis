# Building the Jarvis Single-Folder Bundle

Jarvis can be packaged into a single distributable folder containing the Python
runtime, agent backend, and React web UI. Users untar the archive and run one
launcher — no `uv`, `pnpm`, or system Python needed on the target machine.

## What's inside the bundle

- FastAPI backend (`/run`, `/stream/*`, automations, workflows, artifacts, …)
- React web UI served at `/`
- All LangChain providers (Anthropic, OpenAI, Google GenAI, Ollama, Bedrock)
- LangGraph + SQLite checkpointer
- APScheduler, document extraction (pypdf, python-docx, openpyxl)

## What's NOT inside (and why)

These are excluded to keep the bundle under ~400 MB. The Python code is
guarded so missing modules degrade gracefully (route returns 503, tool returns
a clear "not available" message).

| Feature | Excluded module(s) | What happens when used |
|---|---|---|
| Whisper transcription | `mlx_whisper`, `faster_whisper`, `ctranslate2` | `/transcribe` returns 503 |
| Piper TTS | `piper`, `piper_phonemize` | `/tts` returns 503 |
| Headless browser tool | `browser_use`, `playwright` | `smart_browser` tool returns a stub message |
| Telegram bot | `python-telegram-bot` | Bot disabled (it was already env-var-gated) |
| Discord bot | `discord.py` | Bot disabled (env-var-gated) |
| Finance tools | `yfinance`, `pandas` | Finance tools raise `RuntimeError` |

If you want any of these in the bundle, remove the matching entry from the
`excludes` list in `jarvis.spec` and rebuild.

## Prerequisites

- `uv` (Python package manager)
- `pnpm` (frontend build)
- Native toolchain (Xcode CLT on macOS, `build-essential` on Linux)

## Building

**macOS Apple Silicon:**
```bash
bash scripts/build_macos.sh
```
Output: `dist/jarvis/` and `dist/jarvis-macos-arm64.tar.gz`.

**Linux x86_64:**
```bash
bash scripts/build_linux.sh
```
Output: `dist/jarvis/` and `dist/jarvis-linux-x86_64.tar.gz`.

Cross-compilation is not supported — each platform must build natively.

## Running the bundle

```bash
tar -xzf jarvis-macos-arm64.tar.gz
./jarvis/jarvis start --port 8000
```

Then open `http://127.0.0.1:8000` in a browser.

All CLI subcommands work the same as the source version:
```bash
./jarvis/jarvis start            # web server
./jarvis/jarvis run "query"      # one-shot CLI query
./jarvis/jarvis config list      # view settings
./jarvis/jarvis model list       # view available models
```

## Runtime data

The bundle writes user data to `~/.jarvis/` (configurable via the `WORK_DIR`
env var):

- `database.db` — conversations, messages, artifacts, automations, workflows
- `checkpoints.db` — LangGraph thread state
- `artifacts/`, `documents/` — file storage
- `jarvis.log` — rotating log

Set provider API keys via env vars (`GOOGLE_API_KEY`, `OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`, …) or via `./jarvis/jarvis config set`.

## Troubleshooting

- **`./jarvis/jarvis` exits immediately with no output** — usually a missing
  hidden import. Re-run with `JARVIS_LOG_CONSOLE=1` to see the traceback, then
  add the module to `hidden` in `jarvis.spec`.
- **Bundle is >1 GB** — the `excludes` list missed a heavy dep. Check
  `dist/jarvis/_internal/` for unexpected large folders and add them to the
  excludes.
- **macOS Gatekeeper warning** — the bundle is not signed/notarized. Either
  sign it yourself or right-click → Open the first time.
