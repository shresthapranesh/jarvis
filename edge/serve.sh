#!/usr/bin/env bash
# Run jarvis as the container does: the Rust edge, which starts the Python
# worker when there is work for it and stops it once idle (edge/src/supervisor.rs).
#
# The edge owns the public port (JARVIS_EDGE_BIND, default 127.0.0.1:8000);
# Python listens on loopback only, so nothing reaches it except through the
# edge — which is what keeps the edge's /server-logs peer check meaningful.
#
# JARVIS_WORKER_IDLE is how many idle seconds before Python is stopped
# (default 300; 0 keeps it up, restarting it if it dies). A configured
# Telegram or Discord bot keeps it up regardless. If the edge exits, it stops
# Python first, so a supervisor (docker's restart policy) restarts the pair.
set -u

backend_port="${JARVIS_BACKEND_PORT:-8001}"
export JARVIS_BACKEND_URL="${JARVIS_BACKEND_URL:-http://127.0.0.1:${backend_port}}"
# Run by the edge with `sh -c`, with JARVIS_BACKEND_PORT and JARVIS_EDGE_URL
# set. `exec` so the stop signal reaches uvicorn itself.
export JARVIS_WORKER_CMD="${JARVIS_WORKER_CMD:-exec uvicorn server.entrypoint:app --host 127.0.0.1 --port \"\$JARVIS_BACKEND_PORT\"}"

exec "${JARVIS_EDGE_BIN:-jarvis-edge}"
