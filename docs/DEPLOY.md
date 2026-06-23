# Deploying jarvis (Docker)

jarvis runs an autonomous agent that **executes arbitrary Python** (`run_cell`),
plus shell access, file I/O, and your API tokens. The security model is
**deployment isolation**: the container is the boundary. Run it where a full
compromise is survivable.

## Threat model — read this first

- **The container can be fully owned by prompt injection.** Run it on isolated /
  non-personal hardware (or at least a dedicated VM/container), not on a machine
  holding data you can't afford to lose. Treat the box as compromisable.
- **Everything in the container is reachable by the agent's code** — including
  the secrets in `.env` and the conversation database. The container protects
  your *host*; it does **not** protect the app's own secrets from the code it
  runs. Inject only the secrets this deployment needs, and prefer
  least-privilege, rotatable tokens.
- **The web/GraphQL surface has NO authentication.** `startTask` → `run_cell`
  is effectively unauthenticated RCE for anyone who can reach the port. The
  compose file publishes to `127.0.0.1` only. **Do not** bind `0.0.0.0` or put
  it on a public interface without adding auth (or a trusted authenticating
  reverse proxy) in front.

## Quick start

```bash
cp .env.example .env          # fill in GOOGLE_API_KEY (default model) + any others
docker compose up -d --build
# open http://127.0.0.1:8000
docker compose logs -f jarvis
```

Data (SQLite DBs, artifacts, documents, model cache) persists in the
`jarvis-data` volume across restarts.

## Without compose

```bash
docker build -t jarvis:latest .
docker run -d --name jarvis \
  -p 127.0.0.1:8000:8000 \
  --env-file .env \
  -v jarvis-data:/data \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --memory 4g --cpus 2 \
  jarvis:latest
```

## Post-boot configuration

The config/model CLIs run inside the container and persist to `/data`:

```bash
docker compose exec jarvis python main.py model set-default anthropic:claude-...
docker compose exec jarvis python main.py model list
# Bot allowlists (bots reject everyone until set):
docker compose exec jarvis python main.py config set telegram.allowed_users "123,456"
```

## Configuration notes

- **Default model** is `google_genai:gemma-4-31b-it` → needs `GOOGLE_API_KEY`.
  Override per-conversation in the UI, or change the default with the CLI above.
- **Bots** are enabled by setting `TELEGRAM_BOT_TOKEN` / `DISCORD_BOT_TOKEN` and
  start with the app. They reject all users until you set the allowlists.
- **Ollama on the host** is reachable from the container at
  `host.docker.internal:11434` (the compose `extra_hosts` wires this on Linux
  too). Set `OLLAMA_HOST` accordingly.

## Hardening (already in the compose)

`cap_drop: ALL`, `no-new-privileges`, non-root user (uid 10001), memory/CPU/pids
limits, loopback-only publish. For stricter isolation, uncomment the
`read_only` + `tmpfs` + `shm_size` block in `docker-compose.yml`.

For real egress control (the agent has full outbound network by default), put
the container on a Docker network with an egress allowlist / filtering proxy —
that's where egress is actually enforceable, not inside the kernel.

## Image notes & caveats

- **Apple-Silicon audio (`mlx-whisper`) is skipped** on Linux by design; the
  image uses the cross-platform `faster-whisper` backend (whisper models
  download to `/data/.cache` on first use).
- **Browser tools:** Chromium is installed unless you build with
  `--build-arg INSTALL_BROWSERS=false`. Under `cap_drop: ALL`, Chromium's
  sandbox is unavailable, so browser-use/Playwright must launch with
  `--no-sandbox` (or relax seccomp). Disable browsers if you don't need them —
  it removes ~500MB.
- **TTS:** piper-tts needs a voice file at the path in `PIPER_VOICE`
  (default `voices/...onnx`). Mount one in if you use `/tts`; otherwise audio
  output is the only degraded feature.
- The build needs network (uv + pnpm + Playwright downloads). First build is
  slow; layers cache afterward.
