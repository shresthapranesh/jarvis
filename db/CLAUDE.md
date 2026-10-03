# db/ — models, CRUD, migrations

## Conventions
- Models in `models.py` with `DeclarativeBase`, `Mapped`, `mapped_column`. IDs `str(uuid4())`, timestamps `_now()` (UTC).
- Every ForeignKey column gets `index=True`.
- New tables are created by `create_all` at startup. Changes to existing tables go in `engine.py:_migrate()` (`ALTER TABLE` / `CREATE INDEX IF NOT EXISTS`). The Rust edge reads these tables but never migrates them.
- `async_session` uses `expire_on_commit=False`; never `session.refresh()` after commit.
- `update_*` functions use ORM `setattr`, not raw `UPDATE`, so `onupdate` fires.
- SQLite FK enforcement is **off**; cascades are SQLAlchemy ORM cascades (explicit DELETEs).

## Conversation-scoped resources
Artifacts (row + `.md` under `artifacts_dir`, plus `ArtifactVersion` files) and documents (row + bytes under `documents_dir`). To add one:
- `conversation_id` FK with `index=True`, and a `Conversation` relationship with `cascade="all, delete-orphan"`.
- On-disk bytes: add a `*_dir` field to `core/config.py:AppConfig` and write `{dir}/{id}{ext}`.
- Extend `ops.delete_conversation`: collect file paths **before** the cascade, unlink after commit. It also calls `adelete_thread` on the checkpointer.

Todos live in `thread_state` (below), not on `conversations`.

## Transcript tables (`core/transcript_store.py`)
`thread_messages` (one v1 transcript record per message, `seq`-ordered; `evicted_at` = compacted away, kept), `thread_state` (todos), `transcript_blobs` (media bytes, once per sha256), `kv_store` (what the LangGraph store held). They replace `checkpoints.db`: the agent loop (`core/agent_loop.py`) reads and writes threads here, converting a LangGraph-only thread on first use (`DbThread.load`).
- Write through `apply_messages` — it merges like LangGraph's `add_messages` (same id replaces in place, `RemoveMessage` evicts), checked against it in `tests/test_transcript_store.py`.
- `thread_id` has no FK (automation threads aren't conversations); `delete_conversation` calls `delete_thread`, which also drops blobs no other thread uses.
- `import_checkpoint` / `import_store` convert LangGraph's data once per thread.

## Two SQLite files (`~/.jarvis/`)
- `database.db` — app state; PRAGMAs set per connection in `engine.py:_set_sqlite_pragmas`. `DATABASE_URL` overrides the path.
- `checkpoints.db` — LangGraph threads + store (`thread_id == conversation_id`). Its PRAGMAs (WAL, `busy_timeout=30000`, `synchronous=NORMAL`) are applied by `server/entrypoint.py:_tune_checkpoint_connections`; anything else that opens it must do the same.

## FTS5
`memories_fts`, `document_chunks_fts`, `messages_fts`, `conversation_episodes_fts` are external-content tables kept in sync by triggers (`_ensure_fts()`). Adding one to `_FTS_TABLES` backfills on next boot. Missing FTS5 degrades to dense-only. Never pass raw user text to `MATCH`; use `core/retrieval.py:fts_match_expr()`. `bm25()` is negative, lower is better.

## Checkpoint retention (`core/checkpoint_retention.py`)
LangGraph re-serializes the full state every super-step, so `checkpoints.db` grows quadratically. The hourly online sweep keeps the newest 3 root checkpoints per thread and deletes subgraph namespaces (`tools:<uuid>`). It skips threads in `_tasks` and anything under an hour old (age read from the uuid6 checkpoint id). It does not VACUUM; `main.py maintenance prune-checkpoints` with the server stopped does.

## Model ids in rows
Rows may name a model that was removed from the catalog. Read them through `ops.resolve_model()`; see `core/CLAUDE.md`.
