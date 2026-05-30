import {Link} from '@tanstack/react-router';
import {Suspense, useCallback, useEffect, useMemo, useRef, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ArtifactListQuery} from '../__generated__/ArtifactListQuery.graphql';
import type {ConversationListQuery} from '../__generated__/ConversationListQuery.graphql';
import {artifactListQuery, refreshArtifactList} from '../relay/ArtifactListQuery';
import {conversationListQuery} from '../relay/ConversationListQuery';
import {decodeGlobalId} from '../relay/globalId';
import {ArtifactDetail} from './ArtifactPanel';

const fileIcon = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
    <polyline points="14 2 14 8 20 8" />
  </svg>
);

const closeIcon = (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="18" y1="6" x2="6" y2="18" />
    <line x1="6" y1="6" x2="18" y2="18" />
  </svg>
);

interface Row {
  id: string;
  title: string;
  kind: string;
  conversationId: string | null;
  updatedAt: string;
}

const PANE_MIN = 320;
const PANE_MAX = 900;
const MIN_TABLE = 360;
const WIDTH_KEY = 'artifacts-detail-w';

export function ArtifactsBrowser() {
  // `conversationId: null` returns artifacts across every conversation.
  const data = useLazyLoadQuery<ArtifactListQuery>(
    artifactListQuery,
    {conversationId: null},
    {fetchPolicy: 'store-and-network'},
  );
  // Reused to label each artifact with its conversation title.
  const convData = useLazyLoadQuery<ConversationListQuery>(
    conversationListQuery,
    {},
    {fetchPolicy: 'store-and-network'},
  );

  const convTitles = useMemo(() => {
    const map = new Map<string, string>();
    for (const c of convData.conversations) map.set(decodeGlobalId(c.id), c.title);
    return map;
  }, [convData.conversations]);

  const rows = useMemo<Row[]>(
    () =>
      data.artifacts.map((a) => ({
        id: decodeGlobalId(a.id),
        title: a.title,
        kind: a.kind,
        conversationId: a.conversationId ?? null,
        updatedAt: a.updatedAt,
      })),
    [data.artifacts],
  );

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const selected = rows.find((r) => r.id === selectedId) ?? null;

  // Resizable detail pane — width persisted across reloads.
  const pageRef = useRef<HTMLDivElement>(null);
  const [detailWidth, setDetailWidth] = useState<number>(() => {
    const saved = Number(localStorage.getItem(WIDTH_KEY));
    return saved >= PANE_MIN ? saved : 480;
  });

  useEffect(() => {
    localStorage.setItem(WIDTH_KEY, String(detailWidth));
  }, [detailWidth]);

  const onResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';

    function onMove(ev: MouseEvent) {
      const rect = pageRef.current?.getBoundingClientRect();
      if (!rect) return;
      const max = Math.min(PANE_MAX, rect.width - MIN_TABLE);
      setDetailWidth(Math.min(max, Math.max(PANE_MIN, rect.right - ev.clientX)));
    }
    function onUp() {
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      window.removeEventListener('mousemove', onMove);
      window.removeEventListener('mouseup', onUp);
    }
    window.addEventListener('mousemove', onMove);
    window.addEventListener('mouseup', onUp);
  }, []);

  // Refresh the global (null) list plus the selected artifact's own
  // per-conversation list, so the in-chat ArtifactPanel stays in sync too.
  function refreshLists() {
    return Promise.all([
      refreshArtifactList(null),
      selected?.conversationId ? refreshArtifactList(selected.conversationId) : undefined,
    ]);
  }

  return (
    <div ref={pageRef} className={`artifacts-page${selected ? ' has-detail' : ''}`}>
      <div className="artifacts-table-pane">
        <header className="artifacts-page-header">
          <h1 className="artifacts-page-title">Artifacts</h1>
          <span className="artifacts-page-count">
            {rows.length} {rows.length === 1 ? 'artifact' : 'artifacts'}
          </span>
        </header>

        {rows.length === 0 ? (
          <div className="artifacts-empty">No artifacts yet.</div>
        ) : (
          <div className="artifacts-table-scroll">
            <table className="artifacts-table">
              <thead>
                <tr>
                  <th>Title</th>
                  <th>Conversation</th>
                  <th>Kind</th>
                  <th>Updated</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => {
                  const convTitle = r.conversationId
                    ? convTitles.get(r.conversationId)
                    : undefined;
                  return (
                    <tr
                      key={r.id}
                      className={`artifacts-row${r.id === selectedId ? ' active' : ''}`}
                      onClick={() => setSelectedId(r.id)}
                    >
                      <td className="artifacts-cell-title">
                        <span className="artifacts-cell-icon">{fileIcon}</span>
                        {r.title}
                      </td>
                      <td>
                        {r.conversationId ? (
                          <Link
                            to="/c/$id"
                            params={{id: r.conversationId}}
                            className="artifacts-conv-link"
                            onClick={(e) => e.stopPropagation()}
                          >
                            {convTitle ?? 'Open chat'}
                          </Link>
                        ) : (
                          <span className="artifacts-muted">—</span>
                        )}
                      </td>
                      <td>
                        <span className="artifacts-kind">{r.kind}</span>
                      </td>
                      <td className="artifacts-muted">
                        {new Date(r.updatedAt).toLocaleString()}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {selected && (
        <>
          <div
            className="artifacts-resizer"
            onMouseDown={onResizeStart}
            role="separator"
            aria-orientation="vertical"
            title="Drag to resize"
          />
          <div
            className="artifacts-detail-pane"
            style={{flex: `0 0 ${detailWidth}px`}}
          >
          <div className="artifacts-detail-header">
            <span className="artifacts-detail-name">{selected.title}</span>
            <button
              className="sidebar-close"
              onClick={() => setSelectedId(null)}
              title="Close"
            >
              {closeIcon}
            </button>
          </div>
          <Suspense fallback={<div className="artifact-detail" />}>
            <ArtifactDetail
              key={selected.id}
              rawId={selected.id}
              refreshList={refreshLists}
              onDeleted={() => setSelectedId(null)}
            />
          </Suspense>
          </div>
        </>
      )}
    </div>
  );
}
