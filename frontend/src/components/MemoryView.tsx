import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {marked} from 'marked';
import {useState} from 'react';

import {fetchAgentMemory} from '../relay/AgentMemoryQuery';
import {commitAddMemory} from '../relay/AddMemoryMutation';
import {commitConsolidateMemory} from '../relay/ConsolidateMemoryMutation';
import {commitDeleteMemory} from '../relay/DeleteMemoryMutation';
import {fetchMemories} from '../relay/MemoriesQuery';
import {commitUpdateMemoryItem} from '../relay/UpdateMemoryItemMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {EditIcon, PlusIcon, TrashIcon} from './icons';
import type {Memory, MemoryItem, MemoryKind} from '../lib/types';

const KIND_INFO: Record<MemoryKind, {label: string; hint: string}> = {
  fact: {label: 'Fact', hint: 'surfaced by relevance each turn'},
  core: {label: 'Core', hint: 'in every system prompt'},
};

type Editor = {mode: 'add'} | {mode: 'edit'; item: MemoryItem};

export function MemoryView() {
  const queryClient = useQueryClient();

  const {data: items, isLoading, error} = useQuery<MemoryItem[]>({
    queryKey: ['memories'],
    queryFn: fetchMemories,
  });
  const {data: blob} = useQuery<Memory>({
    queryKey: ['memory'],
    queryFn: fetchAgentMemory,
  });

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [text, setText] = useState('');
  const [kind, setKind] = useState<MemoryKind>('fact');
  const [deleteTarget, setDeleteTarget] = useState<MemoryItem | null>(null);

  const invalidateItems = () => queryClient.invalidateQueries({queryKey: ['memories']});

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

  const addMutation = useMutation({
    mutationFn: () => commitAddMemory(text.trim(), kind),
    onSuccess: async () => {
      await invalidateItems();
      closeEditor();
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const updateMutation = useMutation({
    mutationFn: ({id, text: t}: {id: string; text: string}) =>
      commitUpdateMemoryItem(id, t),
    onSuccess: async () => {
      await invalidateItems();
      closeEditor();
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => commitDeleteMemory(id),
    onSuccess: async () => {
      await invalidateItems();
      setDeleteTarget(null);
      setActionError(null);
    },
    onError: (e: Error) => {
      setDeleteTarget(null);
      setActionError(e.message);
    },
  });

  const consolidateMutation = useMutation({
    mutationFn: () => commitConsolidateMemory(),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({queryKey: ['memories']}),
        queryClient.invalidateQueries({queryKey: ['memory']}),
      ]);
      setActionError(null);
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const all = items ?? [];
  const core = all.filter((m) => m.kind === 'core');
  const facts = all.filter((m) => m.kind === 'fact');
  const showBlobFallback =
    all.length === 0 && !!blob?.exists && blob.content.trim().length > 0;

  function submitEditor() {
    if (!editor) return;
    if (editor.mode === 'add') addMutation.mutate();
    else updateMutation.mutate({id: editor.item.id, text: text.trim()});
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
            onClick={() => consolidateMutation.mutate()}
            disabled={consolidateMutation.isPending}
            title="Run the LLM that extracts new memory items from recent conversations"
          >
            {consolidateMutation.isPending ? 'Consolidating…' : 'Consolidate'}
          </button>
          <button className="artifact-btn primary" onClick={openAdd}>
            <PlusIcon size={14} /> Add memory
          </button>
        </div>
      </header>

      {actionError && !editorOpen && <div className="memory-error">{actionError}</div>}

      {isLoading ? (
        <div className="memory-empty">Loading…</div>
      ) : error ? (
        <div className="memory-empty">
          Failed to load memory: {(error as Error).message}
        </div>
      ) : showBlobFallback ? (
        <div className="memory-section">
          <h2 className="memory-section-title">Legacy memory</h2>
          <p className="memory-subtitle">
            No discrete items yet (no embedding model configured). Showing the legacy{' '}
            <code>AGENTS.md</code> blob. It will be split into items on the next
            consolidation once embeddings are available.
          </p>
          <div
            className="artifact-detail-content agent-bubble"
            dangerouslySetInnerHTML={{__html: marked.parse(blob!.content) as string}}
          />
        </div>
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

      <FormModal
        open={editorOpen}
        title={editor?.mode === 'edit' ? 'Edit memory' : 'Add memory'}
        subtitle="One self-contained fact per item — it's embedded as a whole for retrieval."
        submitLabel={editor?.mode === 'edit' ? 'Save changes' : 'Add memory'}
        submitDisabled={!text.trim()}
        pending={addMutation.isPending || updateMutation.isPending}
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
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
