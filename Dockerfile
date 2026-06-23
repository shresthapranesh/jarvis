# syntax=docker/dockerfile:1.7
#
# Whole-app container for jarvis. The container IS the security boundary: the
# agent runs arbitrary Python via run_cell, so run this on isolated / non-personal
# hardware and treat the box as compromisable. See docs/DEPLOY.md.

############################
# Stage 1 — build frontend #
############################
FROM node:22-bookworm-slim AS frontend
WORKDIR /app/frontend

# Install JS deps first so this layer caches unless the lockfile changes.
COPY frontend/package.json frontend/pnpm-lock.yaml ./
RUN npm install -g pnpm@10 && pnpm install --frozen-lockfile

# Build the Relay/Vite SPA. vite's outDir is ../static/dist → /app/static/dist.
COPY frontend/ ./
RUN pnpm build


#################################
# Stage 2 — python runtime/app  #
#################################
FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS runtime

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    WORK_DIR=/data \
    HF_HOME=/data/.cache/huggingface

# Audio decode for faster-whisper (the Linux transcription backend). Playwright
# pulls its own system libs below via `--with-deps`.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies only (cached unless pyproject/uv.lock change).
# mlx / mlx-whisper / mlx-metal are Apple-Silicon-only; the app falls back to
# faster-whisper on Linux (server/routes_media.py:_USE_MLX), so skip them.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project \
      --no-install-package mlx \
      --no-install-package mlx-whisper \
      --no-install-package mlx-metal

# Headless Chromium for the browser_agent / researcher tools.
# Slim the image by ~500MB with: --build-arg INSTALL_BROWSERS=false
ARG INSTALL_BROWSERS=true
RUN if [ "$INSTALL_BROWSERS" = "true" ]; then \
      playwright install --with-deps chromium \
      && chmod -R a+rx /ms-playwright \
      && rm -rf /var/lib/apt/lists/*; \
    fi

# App source + the prebuilt SPA from stage 1.
COPY . .
COPY --from=frontend /app/static/dist /app/static/dist

# Run unprivileged. /data holds the SQLite DBs + artifacts/documents — mount a
# volume there to persist them across container restarts.
RUN useradd --create-home --uid 10001 appuser \
 && mkdir -p /data \
 && chown -R appuser:appuser /app /data
USER appuser

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health').getcode()==200 else 1)"

# Bind 0.0.0.0 INSIDE the container; publish to 127.0.0.1 on the host (compose).
CMD ["uvicorn", "server.entrypoint:app", "--host", "0.0.0.0", "--port", "8000"]
