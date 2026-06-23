import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {marked} from 'marked';
import {useState} from 'react';

import {fetchAgentMemory} from '../relay/AgentMemoryQuery';
import {commitAddMemory} from '../relay/AddMemoryMutation';
import {commitConsolidateMemory} from '../relay/ConsolidateMemoryMutation';
import {commitDeleteMemory} from '../relay/DeleteMemoryMutation';
import {fetchMemories} from '../relay/MemoriesQuery';
import {commitUpdateMemoryItem} from '../relay/UpdateMemoryItemMutation';
import type {Memory, MemoryItem, MemoryKind} from '../lib/types';

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
  const [newText, setNewText] = useState('');
  const [newKind, setNewKind] = useState<MemoryKind>('fact');
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editText, setEditText] = useState('');

  const invalidateItems = () => queryClient.invalidateQueries({queryKey: ['memories']});

  const addMutation = useMutation({
    mutationFn: () => commitAddMemory(newText.trim(), newKind),
    onSuccess: async () => {
      await invalidateItems();
      setNewText('');
      setActionError(null);
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const updateMutation = useMutation({
    mutationFn: ({id, text}: {id: string; text: string}) => commitUpdateMemoryItem(id, text),
    onSuccess: async () => {
      await invalidateItems();
      setEditingId(null);
      setActionError(null);
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => commitDeleteMemory(id),
    onSuccess: async () => {
      await invalidateItems();
      setActionError(null);
    },
    onError: (e: Error) => setActionError(e.message),
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

  function startEdit(m: MemoryItem) {
    setEditingId(m.id);
    setEditText(m.text);
    setActionError(null);
  }

  function renderItem(m: MemoryItem) {
    if (editingId === m.id) {
      return (
        <li key={m.id} className="memory-item">
          <textarea
            className="memory-item-textarea"
            value={editText}
            onChange={(e) => setEditText(e.target.value)}
            spellCheck={false}
            rows={2}
          />
          <div className="memory-item-actions">
            <button
              className="artifact-btn primary"
              onClick={() => updateMutation.mutate({id: m.id, text: editText.trim()})}
              disabled={updateMutation.isPending || !editText.trim()}
            >
              Save
            </button>
            <button className="artifact-btn" onClick={() => setEditingId(null)}>
              Cancel
            </button>
          </div>
        </li>
      );
    }
    return (
      <li key={m.id} className="memory-item">
        <span className="memory-item-text">{m.text}</span>
        <div className="memory-item-actions">
          <button className="artifact-btn" onClick={() => startEdit(m)}>
            Edit
          </button>
          <button
            className="artifact-btn"
            onClick={() => {
              if (window.confirm('Delete this memory?')) deleteMutation.mutate(m.id);
            }}
          >
            Delete
          </button>
        </div>
      </li>
    );
  }

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
        <button
          className="artifact-btn"
          onClick={() => consolidateMutation.mutate()}
          disabled={consolidateMutation.isPending}
          title="Run the LLM that extracts new memory items from recent conversations"
        >
          {consolidateMutation.isPending ? 'Consolidating…' : 'Consolidate now'}
        </button>
      </header>

      {actionError && <div className="memory-error">{actionError}</div>}

      <div className="memory-add">
        <textarea
          className="memory-item-textarea"
          value={newText}
          onChange={(e) => setNewText(e.target.value)}
          spellCheck={false}
          rows={2}
          placeholder="Add a memory — one self-contained fact…"
        />
        <div className="memory-add-actions">
          <select
            className="memory-kind-select"
            value={newKind}
            onChange={(e) => setNewKind(e.target.value as MemoryKind)}
          >
            <option value="fact">fact</option>
            <option value="core">core</option>
          </select>
          <button
            className="artifact-btn primary"
            onClick={() => addMutation.mutate()}
            disabled={addMutation.isPending || !newText.trim()}
          >
            {addMutation.isPending ? 'Adding…' : 'Add'}
          </button>
        </div>
      </div>

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
            Add one above, ask the agent to <code>remember</code> something, or run
            consolidation after some conversation history accumulates.
          </p>
        </div>
      ) : (
        <>
          <div className="memory-section">
            <h2 className="memory-section-title">
              Core <span className="memory-count">{core.length}</span>
            </h2>
            {core.length === 0 ? (
              <p className="memory-subtitle">No core memories.</p>
            ) : (
              <ul className="memory-list">{core.map(renderItem)}</ul>
            )}
          </div>
          <div className="memory-section">
            <h2 className="memory-section-title">
              Facts <span className="memory-count">{facts.length}</span>
            </h2>
            {facts.length === 0 ? (
              <p className="memory-subtitle">No fact memories.</p>
            ) : (
              <ul className="memory-list">{facts.map(renderItem)}</ul>
            )}
          </div>
        </>
      )}
    </div>
  );
}
