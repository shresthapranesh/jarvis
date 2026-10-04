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
- Extend `ops.delete_conversation`: collect file paths **before** the cascade, unlink after commit. It also deletes the conversation's thread (`delete_thread`). The edge ports it (`edge/src/gql/conversation.rs:delete_conversation`, used by `deleteConversation` and `deleteBoardTask`) — change both.

Todos live in `thread_state` (below), not on `conversations`.

## Transcript tables (`core/transcript_store.py`)
`thread_messages` (one v1 transcript record per message, `seq`-ordered; `evicted_at` = compacted away, kept), `thread_state` (todos), `transcript_blobs` (media bytes, once per sha256), `kv_store` (the key-value store, `KvStore`: memory blob, consolidation watermarks, session state). They replace `checkpoints.db`: the agent loop (`core/agent_loop.py`) reads and writes threads here, converting a LangGraph-only thread on first use (`DbThread.load`).
- Write through `apply_messages` — it merges like LangGraph's `add_messages` (same id replaces in place, `RemoveMessage` evicts), checked against it in `tests/test_transcript_store.py`.
- `thread_id` has no FK (automation threads aren't conversations); `delete_conversation` calls `delete_thread`, which also drops blobs no other thread uses.
- `import_checkpoint` / `import_store_once` convert LangGraph's data: once per thread, and the store once (a marker row, so a deleted key stays deleted). `convert_checkpoints` sweeps every conversation's thread still only in `checkpoints.db`: a `convert_checkpoints` maintenance job queued at server start while there is work (`core/scheduler.py:enqueue_checkpoint_conversion`), or `main.py maintenance convert-checkpoints`. Threads of no conversation (stateless automation runs, deleted conversations) are left behind.

## SQLite files (`~/.jarvis/`)
- `database.db` — everything; PRAGMAs set per connection in `engine.py:_set_sqlite_pragmas`. `DATABASE_URL` overrides the path.
- `checkpoints.db` — legacy, read-only: what LangGraph left. Opened only through `transcript_store.legacy_checkpointer()` to convert a thread still there (needs only `langgraph-checkpoint-sqlite`). Delete the dependency and the conversion code once every install has converted.

## FTS5
`memories_fts`, `document_chunks_fts`, `messages_fts`, `conversation_episodes_fts` are external-content tables kept in sync by triggers (`_ensure_fts()`). Adding one to `_FTS_TABLES` backfills on next boot. Missing FTS5 degrades to dense-only. Never pass raw user text to `MATCH`; use `core/retrieval.py:fts_match_expr()`. `bm25()` is negative, lower is better.

## Model ids in rows
Rows may name a model that was removed from the catalog. Read them through `ops.resolve_model()`; see `core/CLAUDE.md`.
