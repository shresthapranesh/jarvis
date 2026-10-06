# Jarvis

A multi-agent AI research assistant with a web UI. Submit queries and specialized agents collaborate in real-time to produce research, analysis, and reports. Results stream live. Also supports scheduled automations and visual workflow graphs.

## Prerequisites

- [Python 3.13+](https://python.org) with [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Node.js](https://nodejs.org) with [pnpm](https://pnpm.io/installation) (for frontend development only)
- At least one AI provider API key (see below)

## Quick Start

### 1. Clone and install

```bash
git clone <repo-url>
cd jarvis
uv sync
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
uv run main.py start
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

```bash
# Start the web server
uv run main.py start

# Run a one-shot query in the terminal
uv run main.py run "What is the current state of AI chip manufacturing?"

# List saved reports
uv run main.py reports

# View a saved report
uv run main.py view report-name
```

The Rust edge binary carries the same commands without starting Python —
`run`, `config`, `model`, `memory` and `start` (no command serves):

```bash
cd edge && cargo build --release
./target/release/jarvis-edge run "What is the current state of AI chip manufacturing?"
./target/release/jarvis-edge model list
```

### Server options

```bash
uv run main.py start --host 0.0.0.0 --port 8080 --reload
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
uv run main.py start --reload   # auto-reload on changes
```

### Frontend

```bash
cd frontend
pnpm install
pnpm dev        # dev server on :5173, proxies API to :8000
pnpm build      # build to ../static/dist/ for production
```

### Add a new AI model

Edit `core/model_catalog.py` — add a `ModelSpec` entry. The frontend picks it up automatically via `GET /models`.

### Add a new tool

Add a function to `tools/` then import it into the relevant subagent's tool list in `core/agents.py`.

---

## Optional: Browser Automation

Some agent tasks use Playwright for full browser automation. Install the Chromium browser once:

```bash
uv run playwright install chromium
```
