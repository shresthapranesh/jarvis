# jarvis-edge

The Rust front of the jarvis server. Phase 1 of moving off Python: the edge owns
the public port, answers the GraphQL operations that have been ported, and
reverse-proxies everything else to the Python server behind it. The end state
is Rust owning the database, GraphQL, the job queue and the event stream, with
Python reduced to a worker that runs agent jobs. Then an idle box runs no
Python at all.

```
browser / SDK ──▶ edge :8000 ──(ported operation)──▶ SQLite
                       │
                       └──(everything else)──▶ Python :8001
```

## Running it

```bash
# terminal 1 — Python, moved off :8000
uv run uvicorn server.entrypoint:app --reload --port 8001
# terminal 2 — the edge, on :8000 (what vite and the jarvis SDK already target)
cd edge && cargo run
```

Docker runs both via `edge/serve.sh`. Python on its own on :8000 still works,
since the edge is a strict front and nothing in Python depends on it.

| env | default | |
|---|---|---|
| `JARVIS_EDGE_BIND` | `127.0.0.1:8000` | edge listen address |
| `JARVIS_BACKEND_URL` | `http://127.0.0.1:8001` | the Python server |
| `JARVIS_EDGE_LOG` | `info` | `error`…`trace`, the edge's own logs only |
| `DATABASE_URL` / `WORK_DIR` | as `core/config.py` | same database file as Python |

## Routing

An operation is answered by the edge only when **every root field it selects**
is defined in the edge's schema. The schema reads its root fields back from its
own SDL, so the routing table can't drift from the code. Everything else goes
to Python:

- mutations and subscriptions (for now),
- introspection,
- `node(id:)` when the id names a type the edge can't resolve,
- any operation that fails the edge's validation, e.g. it selects a field on an
  owned type that isn't ported. Logged at warn, because it means a type was
  only partly ported.

Splitting one operation across both servers is never attempted.

## Contracts with the Python side

- **Python owns the schema** (`init_db` + `_migrate`). The edge creates no
  tables and opens the same file with the same pragmas, plus
  `foreign_keys = OFF`, which sqlx would otherwise turn on.
- **Wire formats match byte for byte** (`src/gql/codec.rs`):
  - global ids are `base64("Type:id")`
  - `DateTime` is Python's `isoformat()`, which drops a zero fraction
  - message cursors are urlsafe `base64("{iso}|{id}")`
- **`/server-logs` peer check.** Python's localhost-only check sees every
  proxied request as 127.0.0.1, so the edge enforces it on the real peer.

## Porting a domain

1. Port the type and its query resolvers under `src/gql/`, and add the query
   object to `Query` in `src/gql/mod.rs`. Port **every** field of a type: a
   partly ported type makes owned operations fail validation and fall back on
   every call.
2. If the type is a Relay `Node`, add it to `Node` and `NODE_TYPES` in
   `src/gql/node.rs`.
3. Add the frontend operations that are now owned to `PARITY_OPERATIONS` in
   `tests/test_edge_parity.py`, with a test that seeds rows and diffs Python
   against the edge. `test_every_claimed_frontend_query_is_diffed` fails until
   you do.
4. `uv run pytest tests/test_edge_parity.py` and `cargo test` in `edge/`.

Ported so far: `conversations`, `conversation`, `projects`, `project`, and
`node` for Conversation / Message / Project.
