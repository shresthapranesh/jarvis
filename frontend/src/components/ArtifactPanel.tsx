import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {marked} from 'marked';
import {useEffect, useState} from 'react';

import {
  artifactDownloadUrl,
  deleteArtifact,
  fetchArtifact,
  listArtifacts,
  updateArtifact,
} from '../lib/api';
import type {Artifact} from '../lib/types';

interface Props {
  conversationId: string | null;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onClose: () => void;
}

export function ArtifactPanel({conversationId, selectedId, onSelect, onClose}: Props) {
  const queryClient = useQueryClient();

  const {data: artifacts = []} = useQuery<Artifact[]>({
    queryKey: ['artifacts', conversationId],
    queryFn: () => listArtifacts(conversationId),
    enabled: !!conversationId,
  });

  const {data: detail} = useQuery({
    queryKey: ['artifact', selectedId],
    queryFn: () => fetchArtifact(selectedId!),
    enabled: !!selectedId,
  });

  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftContent, setDraftContent] = useState('');
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (detail) {
      setDraftTitle(detail.title);
      setDraftContent(detail.content);
      setEditing(false);
    }
  }, [detail?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  // Auto-select first artifact when none chosen but list is non-empty
  useEffect(() => {
    if (!selectedId && artifacts.length > 0) onSelect(artifacts[0].id);
  }, [selectedId, artifacts, onSelect]);

  const saveMutation = useMutation({
    mutationFn: (body: {title?: string; content?: string}) =>
      updateArtifact(selectedId!, body),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ['artifact', selectedId]});
      await queryClient.invalidateQueries({queryKey: ['artifacts', conversationId]});
      setEditing(false);
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => deleteArtifact(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ['artifacts', conversationId]});
      onSelect(null);
    },
  });

  function copy() {
    if (!detail) return;
    navigator.clipboard.writeText(detail.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="artifact-panel">
      <div className="artifact-panel-header">
        <div className="artifact-panel-title">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          Artifacts
        </div>
        <button className="sidebar-close" onClick={onClose} title="Close">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>

      <div className="artifact-list">
        {artifacts.length === 0 ? (
          <div className="sidebar-empty">No artifacts yet.</div>
        ) : (
          artifacts.map((a) => (
            <button
              key={a.id}
              className={`artifact-list-item${a.id === selectedId ? ' active' : ''}`}
              onClick={() => onSelect(a.id)}
              type="button"
            >
              <span className="artifact-list-title">{a.title}</span>
              <span className="artifact-list-meta">
                {new Date(a.updated_at).toLocaleString()}
              </span>
            </button>
          ))
        )}
      </div>

      {detail && (
        <div className="artifact-detail">
          <div className="artifact-detail-toolbar">
            {editing ? (
              <>
                <button
                  className="artifact-btn primary"
                  onClick={() =>
                    saveMutation.mutate({title: draftTitle, content: draftContent})
                  }
                  disabled={saveMutation.isPending}
                >
                  Save
                </button>
                <button className="artifact-btn" onClick={() => setEditing(false)}>
                  Cancel
                </button>
              </>
            ) : (
              <>
                <button className="artifact-btn" onClick={() => setEditing(true)}>
                  Edit
                </button>
                <button className="artifact-btn" onClick={copy}>
                  {copied ? 'Copied!' : 'Copy'}
                </button>
                <a
                  className="artifact-btn"
                  href={artifactDownloadUrl(detail.id)}
                  download={`${detail.title || detail.id}.md`}
                >
                  Download
                </a>
                <button
                  className="artifact-btn danger"
                  onClick={() => {
                    if (confirm(`Delete "${detail.title}"?`)) {
                      deleteMutation.mutate(detail.id);
                    }
                  }}
                >
                  Delete
                </button>
              </>
            )}
          </div>

          {editing ? (
            <div className="artifact-editor">
              <input
                className="artifact-title-input"
                value={draftTitle}
                onChange={(e) => setDraftTitle(e.target.value)}
                placeholder="Title"
              />
              <textarea
                className="artifact-content-textarea"
                value={draftContent}
                onChange={(e) => setDraftContent(e.target.value)}
                spellCheck={false}
              />
            </div>
          ) : (
            <>
              <h2 className="artifact-detail-title">{detail.title}</h2>
              <div
                className="artifact-detail-content agent-bubble"
                dangerouslySetInnerHTML={{
                  __html: marked.parse(detail.content) as string,
                }}
              />
            </>
          )}
        </div>
      )}
    </div>
  );
}
