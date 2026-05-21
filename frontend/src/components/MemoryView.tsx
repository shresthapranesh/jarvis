import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {marked} from 'marked';
import {useEffect, useState} from 'react';

import {formatRelativeTime} from '../lib/api';
import {fetchAgentMemory} from '../relay/AgentMemoryQuery';
import {commitConsolidateMemory} from '../relay/ConsolidateMemoryMutation';
import {commitUpdateMemory} from '../relay/UpdateMemoryMutation';
import type {Memory} from '../lib/types';

export function MemoryView() {
  const queryClient = useQueryClient();

  const {data: memory, isLoading, error} = useQuery<Memory>({
    queryKey: ['memory'],
    queryFn: fetchAgentMemory,
  });

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState('');
  const [copied, setCopied] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  useEffect(() => {
    if (memory) {
      setDraft(memory.content);
      setEditing(false);
      setActionError(null);
    }
  }, [memory?.modified_at, memory?.exists]); // eslint-disable-line react-hooks/exhaustive-deps

  const saveMutation = useMutation({
    mutationFn: (content: string) => commitUpdateMemory(content),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ['memory']});
      setEditing(false);
      setActionError(null);
    },
    onError: (err: Error) => setActionError(err.message),
  });

  const consolidateMutation = useMutation({
    mutationFn: () => commitConsolidateMemory(),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ['memory']});
      setActionError(null);
    },
    onError: (err: Error) => setActionError(err.message),
  });

  function copy() {
    if (!memory) return;
    navigator.clipboard.writeText(memory.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  function startEditing() {
    setDraft(memory?.content ?? '');
    setEditing(true);
    setActionError(null);
  }

  function cancelEditing() {
    setDraft(memory?.content ?? '');
    setEditing(false);
    setActionError(null);
  }

  return (
    <div className="page memory-page">
      <header className="memory-header">
        <div>
          <h1>Memory</h1>
          <p className="memory-subtitle">
            The agent's persistent <code>AGENTS.md</code> — injected into every system
            prompt. Edited here, written by the agent via{' '}
            <code>write_file("memory/AGENTS.md", …)</code>, or rewritten by the
            scheduled consolidation job.
          </p>
        </div>
        {memory?.modified_at && (
          <span className="memory-meta">
            Last updated {formatRelativeTime(memory.modified_at)}
          </span>
        )}
      </header>

      {isLoading ? (
        <div className="memory-empty">Loading…</div>
      ) : error ? (
        <div className="memory-empty">Failed to load memory: {(error as Error).message}</div>
      ) : (
        <>
          <div className="artifact-detail-toolbar">
            {editing ? (
              <>
                <button
                  className="artifact-btn primary"
                  onClick={() => saveMutation.mutate(draft)}
                  disabled={saveMutation.isPending}
                >
                  {saveMutation.isPending ? 'Saving…' : 'Save'}
                </button>
                <button className="artifact-btn" onClick={cancelEditing}>
                  Cancel
                </button>
              </>
            ) : (
              <>
                <button className="artifact-btn" onClick={startEditing}>
                  Edit
                </button>
                <button
                  className="artifact-btn"
                  onClick={copy}
                  disabled={!memory?.exists}
                >
                  {copied ? 'Copied!' : 'Copy'}
                </button>
                <button
                  className="artifact-btn"
                  onClick={() => consolidateMutation.mutate()}
                  disabled={consolidateMutation.isPending}
                  title="Run the LLM that rewrites memory from recent conversations"
                >
                  {consolidateMutation.isPending ? 'Consolidating…' : 'Consolidate now'}
                </button>
              </>
            )}
          </div>

          {actionError && <div className="memory-error">{actionError}</div>}

          {editing ? (
            <div className="artifact-editor">
              <textarea
                className="artifact-content-textarea"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                spellCheck={false}
                placeholder="# Memory&#10;&#10;Write whatever the agent should remember about you…"
              />
            </div>
          ) : memory && memory.exists ? (
            <div
              className="artifact-detail-content agent-bubble"
              dangerouslySetInnerHTML={{__html: marked.parse(memory.content) as string}}
            />
          ) : (
            <div className="memory-empty">
              <p>No memory yet.</p>
              <p>
                The agent will create one after enough conversation history accumulates,
                or you can write one now.
              </p>
              <button className="artifact-btn primary" onClick={startEditing}>
                Write memory
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
