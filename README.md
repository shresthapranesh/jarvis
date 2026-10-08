# Jarvis

A multi-agent AI research assistant with a web UI. Submit queries and specialized agents collaborate in real-time to produce research, analysis, and reports. Results stream live. Also supports scheduled automations and visual workflow graphs.

## Prerequisites

- [Rust](https://rustup.rs) (the server)
- [Python 3.13+](https://python.org) with [uv](https://docs.astral.sh/uv/getting-started/installation/) (the agent's notebooks)
- [Node.js](https://nodejs.org) with [pnpm](https://pnpm.io/installation) (to build the web UI)
- At least one AI provider API key (see below)

## Quick Start

### 1. Clone and install

```bash
git clone <repo-url>
cd jarvis
uv sync
(cd frontend && pnpm install && pnpm build)
(cd edge && cargo build --release)
```

### 2. Set up environment variables

Create a `.env` file in the project root:

```env
# Pick at least one provider
GOOGLE_API_KEY=your_google_api_key

# AWS Bedrock (optional)
AWS_ACCESS_KEY_ID=your_access_key
AWS_SECRET_ACCESS_KEY=your_secret_key
AWS_DEFAULT_REGION=us-east-1
```

> **Google API key**: Get one at [aistudio.google.com](https://aistudio.google.com). The default model is Gemma 4 31B which is free.

### 3. Start the server

```bash
JARVIS_APP_DIR=. edge/target/release/jarvis-edge
```

Open [http://localhost:8000](http://localhost:8000) in your browser.

---

## AI Providers

| Provider | Models | Setup |
|----------|--------|-------|
| **Google AI** (default) | Gemma 4 31B/26B, Gemini 2.5 Pro, Gemini 2.0 Flash | `GOOGLE_API_KEY` |
| **Ollama** | Gemma4, Llama 3.3, Qwen3 | [Install Ollama](https://ollama.com) + `ollama pull <model>` |
| **AWS Bedrock** | Claude Sonnet 4.6 | `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` + `AWS_DEFAULT_REGION` |

---

## Features

### Chat
Send messages in the web UI. Agents can search the web, run Python code, read files, and fetch financial data. Responses stream in real-time.

### Automations
Schedule recurring tasks with a cron expression (e.g. `0 9 * * 1-5` for weekday mornings). Three input types:
- **Prompt** — runs the agent on a fixed query
- **Code** — executes a Python script
- **Webhook** — fires an HTTP request

### Workflows
Build visual multi-step pipelines in the Workflows tab. Supports agent nodes, conditional branches, and parallel map nodes.

---

## CLI Usage

The server binary is the command line too (no command serves):

```bash
edge/target/release/jarvis-edge run "What is the current state of AI chip manufacturing?"
edge/target/release/jarvis-edge model list
edge/target/release/jarvis-edge config set <key> <value>
edge/target/release/jarvis-edge start --host 0.0.0.0 --port 8080
```

---

## Configuration

All settings can be set via environment variables or a `.env` file:

| Variable | Default | Description |
|----------|---------|-------------|
| `WORK_DIR` | `~/.jarvis` | Directory for databases and memory files |
| `DATABASE_URL` | `sqlite:///$WORK_DIR/database.db` | SQLite database path |

---

## Development

### Backend

```bash
cd edge && JARVIS_APP_DIR=.. cargo run   # the server on :8000 — see edge/README.md
```

### Frontend

```bash
cd frontend
pnpm install
pnpm dev        # dev server on :5173, proxies API to :8000
pnpm build      # build to ../static/dist/ for production
```

### Add a new AI model

Settings → Models in the UI, or `jarvis-edge model add`. A built-in one goes in `core/builtin_models.json` (compiled into the server — rebuild it).

### Add a new tool

Add a function to the `jarvis` SDK (`tools/sdk.py`), which the agent calls from its notebook. See `tools/CLAUDE.md`.

---

## Optional: Browser Automation

Some agent tasks use Playwright for full browser automation. Install the Chromium browser once:

```bash
uv run playwright install chromium
```
