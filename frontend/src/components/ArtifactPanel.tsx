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

const svg = (path: React.ReactNode, w = 2) => (
  <svg
    width="14"
    height="14"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth={w}
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    {path}
  </svg>
);

const ICON = {
  edit: svg(
    <>
      <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
      <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
    </>,
  ),
  copy: svg(
    <>
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </>,
  ),
  check: svg(<polyline points="20 6 9 17 4 12" />, 2.5),
  download: svg(
    <>
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4" />
      <polyline points="7 10 12 15 17 10" />
      <line x1="12" y1="15" x2="12" y2="3" />
    </>,
  ),
  trash: svg(
    <>
      <polyline points="3 6 5 6 21 6" />
      <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
      <path d="M10 11v6M14 11v6" />
      <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
    </>,
  ),
  close: svg(
    <>
      <line x1="18" y1="6" x2="6" y2="18" />
      <line x1="6" y1="6" x2="18" y2="18" />
    </>,
    2.5,
  ),
};

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
            refreshList={() => refreshArtifactList(conversationId)}
            onDeleted={() => onSelect(null)}
          />
        </Suspense>
      )}
    </div>
  );
}

interface DetailProps {
  rawId: string;
  /** Refetches whichever artifact list(s) this detail is shown within. */
  refreshList: () => Promise<unknown> | void;
  onDeleted: () => void;
}

export function ArtifactDetail({rawId, refreshList, onDeleted}: DetailProps) {
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
      await Promise.all([refreshArtifactDetail(rawId), refreshList()]);
      setEditing(false);
    } finally {
      setSaving(false);
    }
  }

  async function remove() {
    if (!detail) return;
    if (!confirm(`Delete "${detail.title}"?`)) return;
    await commitDeleteArtifact(rawId);
    await refreshList();
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
              {ICON.check}
              <span>{saving ? 'Saving…' : 'Save'}</span>
            </button>
            <button className="artifact-btn" onClick={() => setEditing(false)}>
              {ICON.close}
              <span>Cancel</span>
            </button>
          </>
        ) : (
          <>
            <button className="artifact-btn" onClick={() => setEditing(true)}>
              {ICON.edit}
              <span>Edit</span>
            </button>
            <button
              className={`artifact-btn${copied ? ' success' : ''}`}
              onClick={copy}
            >
              {copied ? ICON.check : ICON.copy}
              <span>{copied ? 'Copied' : 'Copy'}</span>
            </button>
            <a
              className="artifact-btn"
              href={artifactDownloadUrl(rawId)}
              download={`${detail.title || rawId}.md`}
            >
              {ICON.download}
              <span>Download</span>
            </a>
            <button className="artifact-btn danger" onClick={remove}>
              {ICON.trash}
              <span>Delete</span>
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
