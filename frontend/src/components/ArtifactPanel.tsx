import {marked} from 'marked';
import {Suspense, useEffect, useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ArtifactDetailQuery} from '../__generated__/ArtifactDetailQuery.graphql';
import type {ArtifactListQuery} from '../__generated__/ArtifactListQuery.graphql';
import {artifactDownloadUrl} from '../lib/api';
import {artifactDetailQuery, refreshArtifactDetail} from '../relay/ArtifactDetailQuery';
import {artifactListQuery, refreshArtifactList} from '../relay/ArtifactListQuery';
import {commitDeleteArtifact} from '../relay/DeleteArtifactMutation';
import {decodeGlobalId, encodeGlobalId} from '../relay/globalId';
import {commitUpdateArtifact} from '../relay/UpdateArtifactMutation';

interface Props {
  conversationId: string;
  selectedId: string | null;
  onSelect: (id: string | null) => void;
  onClose: () => void;
}

export function ArtifactPanel({conversationId, selectedId, onSelect, onClose}: Props) {
  const data = useLazyLoadQuery<ArtifactListQuery>(
    artifactListQuery,
    {conversationId},
    {fetchPolicy: 'store-and-network'},
  );

  const artifacts = useMemo(
    () =>
      data.artifacts.map((a) => ({
        id: decodeGlobalId(a.id),
        title: a.title,
        updatedAt: a.updatedAt,
      })),
    [data.artifacts],
  );

  // Auto-select first artifact when none chosen but list is non-empty
  useEffect(() => {
    if (!selectedId && artifacts.length > 0) onSelect(artifacts[0].id);
  }, [selectedId, artifacts, onSelect]);

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
                {new Date(a.updatedAt).toLocaleString()}
              </span>
            </button>
          ))
        )}
      </div>

      {selectedId && (
        <Suspense fallback={<div className="artifact-detail" />}>
          <ArtifactDetail
            key={selectedId}
            rawId={selectedId}
            conversationId={conversationId}
            onDeleted={() => onSelect(null)}
          />
        </Suspense>
      )}
    </div>
  );
}

interface DetailProps {
  rawId: string;
  conversationId: string;
  onDeleted: () => void;
}

function ArtifactDetail({rawId, conversationId, onDeleted}: DetailProps) {
  const data = useLazyLoadQuery<ArtifactDetailQuery>(
    artifactDetailQuery,
    {id: encodeGlobalId('Artifact', rawId)},
    {fetchPolicy: 'store-and-network'},
  );
  const detail = data.artifact;

  const [editing, setEditing] = useState(false);
  const [draftTitle, setDraftTitle] = useState('');
  const [draftContent, setDraftContent] = useState('');
  const [copied, setCopied] = useState(false);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (detail) {
      setDraftTitle(detail.title);
      setDraftContent(detail.content);
      setEditing(false);
    }
  }, [detail?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  if (!detail) return null;

  async function save() {
    if (!detail) return;
    setSaving(true);
    try {
      await commitUpdateArtifact(rawId, {title: draftTitle, content: draftContent});
      await Promise.all([refreshArtifactDetail(rawId), refreshArtifactList(conversationId)]);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!detail) return;
    if (!confirm(`Delete "${detail.title}"?`)) return;
    await commitDeleteArtifact(rawId);
    await refreshArtifactList(conversationId);
    onDeleted();
  }

  function copy() {
    if (!detail) return;
    navigator.clipboard.writeText(detail.content);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  return (
    <div className="artifact-detail">
      <div className="artifact-detail-toolbar">
        {editing ? (
          <>
            <button className="artifact-btn primary" onClick={save} disabled={saving}>
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
              href={artifactDownloadUrl(rawId)}
              download={`${detail.title || rawId}.md`}
            >
              Download
            </a>
            <button className="artifact-btn danger" onClick={remove}>
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
            dangerouslySetInnerHTML={{__html: marked.parse(detail.content) as string}}
          />
        </>
      )}
    </div>
  );
}
