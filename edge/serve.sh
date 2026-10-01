#!/usr/bin/env bash
# Run the Python server behind the Rust edge, as the container does.
#
# The edge owns the public port (JARVIS_EDGE_BIND, default 127.0.0.1:8000);
# Python listens on loopback only, so nothing reaches it except through the
# edge — which is what keeps the edge's /server-logs peer check meaningful.
#
# If either process exits, the other is stopped and the script exits with its
# status, so a supervisor (docker's restart policy) restarts the pair.
set -u

backend_port="${JARVIS_BACKEND_PORT:-8001}"
export JARVIS_BACKEND_URL="${JARVIS_BACKEND_URL:-http://127.0.0.1:${backend_port}}"

uvicorn server.entrypoint:app --host 127.0.0.1 --port "${backend_port}" &
"${JARVIS_EDGE_BIN:-jarvis-edge}" &

trap 'kill -TERM $(jobs -p) 2>/dev/null' TERM INT
wait -n
status=$?
kill -TERM $(jobs -p) 2>/dev/null
wait
exit "${status}"
