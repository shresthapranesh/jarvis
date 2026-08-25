import {marked} from 'marked';
import {useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {AgentMemoryQuery as TAgentMemoryQuery} from '../__generated__/AgentMemoryQuery.graphql';
import type {MemoriesQuery as TMemoriesQuery} from '../__generated__/MemoriesQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import type {MemoryItem, MemoryKind} from '../lib/types';
import {commitAddMemory} from '../relay/AddMemoryMutation';
import {agentMemoryQuery, refreshAgentMemory} from '../relay/AgentMemoryQuery';
import {commitConsolidateMemory} from '../relay/ConsolidateMemoryMutation';
import {commitDeleteAgentMemory} from '../relay/DeleteAgentMemoryMutation';
import {commitDeleteMemory} from '../relay/DeleteMemoryMutation';
import {memoriesQuery, refreshMemories} from '../relay/MemoriesQuery';
import {commitUpdateMemory} from '../relay/UpdateMemoryMutation';
import {commitUpdateMemoryItem} from '../relay/UpdateMemoryItemMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {EditIcon, PlusIcon, TrashIcon} from './icons';
import {useQueryRetry} from './QueryBoundary';

const KIND_INFO: Record<MemoryKind, {label: string; hint: string}> = {
  fact: {label: 'Fact', hint: 'surfaced by relevance each turn'},
  core: {label: 'Core', hint: 'in every system prompt'},
};

type Editor = {mode: 'add'} | {mode: 'edit'; item: MemoryItem};

export function MemoryView() {
  // Suspends on first load and throws on failure — the /memory route wraps this
  // in a QueryBoundary, which supplies the retry fetchKey.
  const fetchKey = useQueryRetry();
  const memoryData = useLazyLoadQuery<TMemoriesQuery>(
    memoriesQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey},
  );
  const blobData = useLazyLoadQuery<TAgentMemoryQuery>(
    agentMemoryQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey},
  );

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [text, setText] = useState('');
  const [kind, setKind] = useState<MemoryKind>('fact');
  const [deleteTarget, setDeleteTarget] = useState<MemoryItem | null>(null);

  function closeEditor() {
    setEditor(null);
    setActionError(null);
  }

  function openAdd() {
    setText('');
    setKind('fact');
    setActionError(null);
    setEditor({mode: 'add'});
  }

  function openEdit(m: MemoryItem) {
    setText(m.text);
    setActionError(null);
    setEditor({mode: 'edit', item: m});
  }

  // Each write leaves the Relay store correct via its own updater (or, for an
  // edit, via plain normalization), so none of these refetch the list.
  const addAction = useAsyncAction(() => commitAddMemory(text.trim(), kind), {
    onSuccess: closeEditor,
    onError: (e) => setActionError(e.message),
  });

  const updateAction = useAsyncAction((id: string, t: string) => commitUpdateMemoryItem(id, t), {
    onSuccess: closeEditor,
    onError: (e) => setActionError(e.message),
  });

  const deleteAction = useAsyncAction((id: string) => commitDeleteMemory(id), {
    onSuccess: () => {
      setDeleteTarget(null);
      setActionError(null);
    },
    onError: (e) => {
      setDeleteTarget(null);
      setActionError(e.message);
    },
  });

  // The exception: consolidation rewrites the whole set server-side, so there is
  // no delta to apply — re-read both queries into the store.
  const consolidateAction = useAsyncAction(
    async () => {
      await commitConsolidateMemory();
      await Promise.all([refreshMemories(), refreshAgentMemory()]);
    },
    {
      onSuccess: () => setActionError(null),
      onError: (e) => setActionError(e.message),
    },
  );

  const all: MemoryItem[] = memoryData.memories.map((m) => ({
    id: m.id,
    kind: m.kind as MemoryKind,
    text: m.text,
    updated_at: m.updatedAt,
  }));
  const core = all.filter((m) => m.kind === 'core');
  const facts = all.filter((m) => m.kind === 'fact');
  const blob = blobData.agentMemory;
  const showBlobFallback = all.length === 0 && blob.exists && blob.content.trim().length > 0;

  function submitEditor() {
    if (!editor) return;
    if (editor.mode === 'add') void addAction.run();
    else void updateAction.run(editor.item.id, text.trim());
  }

  function renderItem(m: MemoryItem) {
    return (
      <li key={m.id} className={`memory-item memory-item--${m.kind}`}>
        <div className="memory-item-main">
          <span className="memory-item-text">{m.text}</span>
          <span className="memory-item-meta">
            Updated {new Date(m.updated_at).toLocaleDateString()}
          </span>
        </div>
        <div className="memory-item-actions">
          <button className="icon-btn" title="Edit memory" onClick={() => openEdit(m)}>
            <EditIcon size={14} />
          </button>
          <button
            className="icon-btn icon-btn--danger"
            title="Delete memory"
            onClick={() => setDeleteTarget(m)}
          >
            <TrashIcon size={14} />
          </button>
        </div>
      </li>
    );
  }

  function renderSection(title: string, kindKey: MemoryKind, list: MemoryItem[]) {
    return (
      <section className="memory-section">
        <h2 className="memory-section-title">
          <span className={`memory-kind-dot memory-kind-dot--${kindKey}`} />
          {title} <span className="memory-count">{list.length}</span>
          <span className="memory-section-hint">{KIND_INFO[kindKey].hint}</span>
        </h2>
        {list.length === 0 ? (
          <p className="memory-section-empty">Nothing here yet.</p>
        ) : (
          <ul className="memory-list">{list.map(renderItem)}</ul>
        )}
      </section>
    );
  }

  const editorOpen = editor !== null;
  const editKind = editor?.mode === 'edit' ? editor.item.kind : kind;

  return (
    <div className="page memory-page">
      <header className="memory-header">
        <div>
          <h1>Memory</h1>
          <p className="memory-subtitle">
            The agent's long-term memory — discrete items embedded for retrieval.{' '}
            <strong>Core</strong> items are injected into every system prompt;{' '}
            <strong>fact</strong> items are surfaced by relevance each turn. The agent can
            also write them via <code>remember(…)</code>, or the scheduled consolidation
            job extracts them from recent conversations.
          </p>
        </div>
        <div className="memory-header-actions">
          <button
            className="artifact-btn"
            onClick={() => void consolidateAction.run()}
            disabled={consolidateAction.pending}
            title="Run the LLM that extracts new memory items from recent conversations"
          >
            {consolidateAction.pending ? 'Consolidating…' : 'Consolidate'}
          </button>
          <button className="artifact-btn primary" onClick={openAdd}>
            <PlusIcon size={14} /> Add memory
          </button>
        </div>
      </header>

      {actionError && !editorOpen && <div className="memory-error">{actionError}</div>}

      {showBlobFallback ? (
        <LegacyBlob blob={blob} primary />
      ) : all.length === 0 ? (
        <div className="memory-empty">
          <p>No memories yet.</p>
          <p>
            Add one, ask the agent to <code>remember</code> something, or run
            consolidation after some conversation history accumulates.
          </p>
          <button className="artifact-btn primary" onClick={openAdd}>
            <PlusIcon size={14} /> Add memory
          </button>
        </div>
      ) : (
        <>
          {renderSection('Core', 'core', core)}
          {renderSection('Facts', 'fact', facts)}
        </>
      )}

      {/* The blob is still the whole of memory on a keyless setup, and the only
          copy of anything consolidation hasn't split yet — so it needs the same
          view / replace / clear the `memory` CLI subcommands give it, not just a
          read-only fallback when the discrete list happens to be empty. */}
      {!showBlobFallback && blob.exists && <LegacyBlob blob={blob} />}

      <FormModal
        open={editorOpen}
        title={editor?.mode === 'edit' ? 'Edit memory' : 'Add memory'}
        subtitle="One self-contained fact per item — it's embedded as a whole for retrieval."
        submitLabel={editor?.mode === 'edit' ? 'Save changes' : 'Add memory'}
        submitDisabled={!text.trim()}
        pending={addAction.pending || updateAction.pending}
        error={actionError}
        onSubmit={submitEditor}
        onClose={closeEditor}
      >
        <div className="auto-form-group">
          <span className="auto-form-label">Memory</span>
          <textarea
            className="auto-form-textarea"
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
            rows={4}
            autoFocus
            placeholder="e.g. Prefers responses in Spanish when discussing travel."
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Kind</span>
          {editor?.mode === 'edit' ? (
            <span className="memory-kind-static">
              <span className={`memory-kind-dot memory-kind-dot--${editKind}`} />
              {KIND_INFO[editKind].label} — {KIND_INFO[editKind].hint}
            </span>
          ) : (
            <div className="seg" role="radiogroup" aria-label="Memory kind">
              {(Object.keys(KIND_INFO) as MemoryKind[]).map((k) => (
                <button
                  key={k}
                  type="button"
                  role="radio"
                  aria-checked={kind === k}
                  className={`seg-opt${kind === k ? ' seg-opt--active' : ''}`}
                  onClick={() => setKind(k)}
                >
                  <span className="seg-opt-label">
                    <span className={`memory-kind-dot memory-kind-dot--${k}`} />
                    {KIND_INFO[k].label}
                  </span>
                  <span className="seg-opt-hint">{KIND_INFO[k].hint}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </FormModal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete memory"
        message={
          <p>
            This removes the item from the agent's memory permanently:{' '}
            <strong>
              {deleteTarget && deleteTarget.text.length > 120
                ? `${deleteTarget.text.slice(0, 120)}…`
                : deleteTarget?.text}
            </strong>
          </p>
        }
        confirmLabel="Delete"
        danger
        onConfirm={() => deleteTarget && void deleteAction.run(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

function LegacyBlob({
  blob,
  primary = false,
}: {
  blob: TAgentMemoryQuery['response']['agentMemory'];
  primary?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(blob.content);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await commitUpdateMemory(draft);
      await refreshAgentMemory();
      setEditing(false);
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    setBusy(true);
    setError(null);
    try {
      await commitDeleteAgentMemory();
      await refreshAgentMemory();
      setDraft('');
      setEditing(false);
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setBusy(false);
      setConfirmClear(false);
    }
  }

  return (
    <div className="memory-section">
      <h2 className="memory-section-title">
        Legacy AGENTS.md
        <span className="memory-section-hint">{blob.content.length} chars</span>
      </h2>
      <p className="memory-subtitle">
        {primary ? (
          <>
            No discrete items yet (no embedding model configured), so this blob <em>is</em> the
            agent&apos;s memory. It will be split into items on the next consolidation once
            embeddings are available.
          </>
        ) : (
          <>
            The free-text blob kept in the LangGraph store — the keyless fallback, and whatever
            predates the split into discrete items. Same entry as{' '}
            <code>main.py memory show/set/reset</code>.
          </>
        )}
      </p>

      {error && <div className="memory-error">{error}</div>}

      {editing ? (
        <>
          <textarea
            className="config-input config-input--multiline"
            rows={16}
            value={draft}
            disabled={busy}
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="config-actions">
            <button
              className="artifact-btn primary"
              disabled={busy || !draft.trim()}
              title={draft.trim() ? '' : 'Use Clear to remove the entry — an empty blob is not the same as no blob.'}
              onClick={() => void save()}
            >
              Save
            </button>
            <button
              className="artifact-btn"
              disabled={busy}
              onClick={() => {
                setDraft(blob.content);
                setEditing(false);
                setError(null);
              }}
            >
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <div
            className="artifact-detail-content agent-bubble"
            dangerouslySetInnerHTML={{__html: marked.parse(blob.content) as string}}
          />
          <div className="config-actions">
            <button
              className="artifact-btn"
              onClick={() => {
                setDraft(blob.content);
                setEditing(true);
              }}
            >
              <EditIcon size={14} /> Edit
            </button>
            <button
              className="artifact-btn"
              disabled={!blob.exists}
              onClick={() => setConfirmClear(true)}
            >
              <TrashIcon size={14} /> Clear
            </button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirmClear}
        title="Clear AGENTS.md memory"
        message="Deletes the blob entry outright. The agent falls back to its system prompt alone. Discrete memory items are untouched."
        confirmLabel="Clear"
        danger
        onConfirm={() => void clear()}
        onCancel={() => setConfirmClear(false)}
      />
    </div>
  );
}
