import * as stylex from '@stylexjs/stylex';
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
import {commitRestoreArtifactVersion} from '../relay/RestoreArtifactVersionMutation';
import {commitUpdateArtifact} from '../relay/UpdateArtifactMutation';
import {detail as detailStyles, diff, editor, panel, version} from './ArtifactPanel.styles';
import {btn, closeBtn, prose} from './ui';

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
    <div {...stylex.props(panel.root)}>
      <div {...stylex.props(panel.header)}>
        <div {...stylex.props(panel.title)}>
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          Artifacts
        </div>
        <button {...stylex.props(closeBtn.base)} onClick={onClose} title="Close">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>

      <div {...stylex.props(panel.list)}>
        {artifacts.length === 0 ? (
          <div {...stylex.props(panel.empty)}>No artifacts yet.</div>
        ) : (
          artifacts.map((a) => (
            <button
              key={a.id}
              {...stylex.props(panel.item, a.id === selectedId && panel.itemActive)}
              onClick={() => onSelect(a.id)}
              type="button"
            >
              <span {...stylex.props(panel.itemTitle)}>{a.title}</span>
              <span {...stylex.props(panel.itemMeta)}>
                {new Date(a.updatedAt).toLocaleString()}
              </span>
            </button>
          ))
        )}
      </div>

      {selectedId && (
        <Suspense fallback={<div {...stylex.props(detailStyles.root)} />}>
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

  const versions = (detail as any).versions as Array<{
    id: string;
    version: number;
    title: string;
    createdAt: string;
    content: string;
  }>;
  const versionCount = (detail as any).versionCount ?? versions?.length ?? 0;

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

  // ── Version / diff state ──────────────────────────────────────────
  const [showVersions, setShowVersions] = useState(false);
  const [compareFrom, setCompareFrom] = useState<number | null>(null);
  const [compareTo, setCompareTo] = useState<number | null>(null);
  const [restoring, setRestoring] = useState<number | null>(null);

  async function restoreVersion(v: number) {
    if (!confirm(`Restore version ${v} as new current version?`)) return;
    setRestoring(v);
    try {
      await commitRestoreArtifactVersion(rawId, v);
      await Promise.all([refreshArtifactDetail(rawId), refreshList()]);
    } finally {
      setRestoring(null);
    }
  }

  // Simple line diff (Myers-ish via LCS DP, limited to 2000 lines to stay cheap)
  function lineDiff(a: string, b: string): Array<{type: 'same' | 'add' | 'del'; text: string}> {
    const aLines = a.split('\n');
    const bLines = b.split('\n');
    const n = aLines.length;
    const m = bLines.length;
    // Clamp to avoid O(n*m) blow up on huge artifacts
    if (n > 2000 || m > 2000) {
      return [
        {
          type: 'same',
          text: `Diff too large to display (${n} vs ${m} lines). Showing full new content.`,
        },
        ...bLines.map((l) => ({type: 'same' as const, text: l})),
      ];
    }
    const dp: number[][] = Array.from({length: n + 1}, () => Array(m + 1).fill(0));
    for (let i = n - 1; i >= 0; i--) {
      for (let j = m - 1; j >= 0; j--) {
        dp[i][j] =
          aLines[i] === bLines[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
    const out: Array<{type: 'same' | 'add' | 'del'; text: string}> = [];
    let i = 0,
      j = 0;
    while (i < n || j < m) {
      if (i < n && j < m && aLines[i] === bLines[j]) {
        out.push({type: 'same', text: aLines[i]});
        i++;
        j++;
      } else if (j < m && (i >= n || dp[i][j + 1] >= dp[i + 1][j])) {
        out.push({type: 'add', text: bLines[j]});
        j++;
      } else if (i < n) {
        out.push({type: 'del', text: aLines[i]});
        i++;
      }
    }
    return out;
  }

  function getVersionContent(vNum: number | null): string {
    if (vNum === null) return (detail as any).content as string;
    const found = versions?.find((v) => v.version === vNum);
    return found?.content ?? '';
  }

  const diffRows = (() => {
    if (compareFrom === null || compareTo === null) return null;
    const fromContent = getVersionContent(compareFrom);
    const toContent = getVersionContent(compareTo === -1 ? null : compareTo);
    return lineDiff(fromContent, toContent);
  })();

  return (
    <div {...stylex.props(detailStyles.root)}>
      <div {...stylex.props(detailStyles.toolbar)}>
        {editing ? (
          <>
            <button {...stylex.props(btn.base, btn.primary)} onClick={save} disabled={saving}>
              {ICON.check}
              <span>{saving ? 'Saving…' : 'Save'}</span>
            </button>
            <button {...stylex.props(btn.base)} onClick={() => setEditing(false)}>
              {ICON.close}
              <span>Cancel</span>
            </button>
          </>
        ) : (
          <>
            {detail.kind === 'markdown' && (
              <>
                <button {...stylex.props(btn.base)} onClick={() => setEditing(true)}>
                  {ICON.edit}
                  <span>Edit</span>
                </button>
                <button {...stylex.props(btn.base, copied && btn.success)} onClick={copy}>
                  {copied ? ICON.check : ICON.copy}
                  <span>{copied ? 'Copied' : 'Copy'}</span>
                </button>
              </>
            )}
            <a
              {...stylex.props(btn.base)}
              href={artifactDownloadUrl(rawId)}
              download={`${detail.title || rawId}.md`}
            >
              {ICON.download}
              <span>Download</span>
            </a>
            <button {...stylex.props(btn.base)} onClick={() => setShowVersions(!showVersions)}>
              <span>
                🕑 {versionCount}v{showVersions ? ' ▲' : ' ▼'}
              </span>
            </button>
            <button {...stylex.props(btn.base, btn.danger)} onClick={remove}>
              {ICON.trash}
              <span>Delete</span>
            </button>
          </>
        )}
      </div>

      {showVersions && (
        <div {...stylex.props(version.panel)}>
          <div {...stylex.props(version.header)}>
            <strong>Version history</strong>
            <span {...stylex.props(version.hint)}>
              Compare any two versions, restore old as new
            </span>
          </div>
          <div {...stylex.props(version.list)}>
            {/* Current */}
            <div {...stylex.props(version.row, compareTo === -1 && version.rowActive)}>
              <span {...stylex.props(version.badge)}>current</span>
              <span {...stylex.props(version.title)}>{detail.title}</span>
              <span {...stylex.props(version.meta)}>
                {new Date((detail as any).updatedAt).toLocaleString()}
              </span>
              <div {...stylex.props(version.actions)}>
                <button
                  {...stylex.props(btn.base, btn.small)}
                  onClick={() =>
                    setCompareFrom(
                      compareFrom === null
                        ? (versions?.[versions.length - 1]?.version ?? null)
                        : null,
                    )
                  }
                >
                  from
                </button>
                <button
                  {...stylex.props(btn.base, btn.small, btn.primary)}
                  onClick={() => setCompareTo(-1)}
                >
                  to
                </button>
              </div>
            </div>
            {(versions || [])
              .slice()
              .reverse()
              .map((v) => (
                <div
                  key={v.version}
                  {...stylex.props(
                    version.row,
                    (compareFrom === v.version || compareTo === v.version) && version.rowActive,
                  )}
                >
                  <span {...stylex.props(version.badge)}>v{v.version}</span>
                  <span {...stylex.props(version.title)}>{v.title}</span>
                  <span {...stylex.props(version.meta)}>
                    {new Date(v.createdAt).toLocaleString()}
                  </span>
                  <div {...stylex.props(version.actions)}>
                    <button
                      {...stylex.props(btn.base, btn.small)}
                      onClick={() => setCompareFrom(v.version)}
                      title="Set as diff source"
                    >
                      from
                    </button>
                    <button
                      {...stylex.props(btn.base, btn.small, btn.primary)}
                      onClick={() => setCompareTo(v.version)}
                      title="Set as diff target"
                    >
                      to
                    </button>
                    <button
                      {...stylex.props(btn.base, btn.small)}
                      disabled={restoring === v.version}
                      onClick={() => restoreVersion(v.version)}
                    >
                      {restoring === v.version ? '…' : 'restore'}
                    </button>
                  </div>
                </div>
              ))}
          </div>

          {compareFrom !== null && compareTo !== null && diffRows && (
            <div {...stylex.props(diff.root)}>
              <div {...stylex.props(diff.header)}>
                Diff: v{compareFrom} → {compareTo === -1 ? 'current' : `v${compareTo}`}
                <button
                  {...stylex.props(btn.base, btn.small)}
                  onClick={() => {
                    setCompareFrom(null);
                    setCompareTo(null);
                  }}
                >
                  clear
                </button>
              </div>
              <pre {...stylex.props(diff.content)}>
                {diffRows.map((row, idx) => (
                  <div
                    key={idx}
                    {...stylex.props(
                      row.type === 'add' ? diff.add : row.type === 'del' ? diff.del : null,
                    )}
                  >
                    {row.type === 'add' ? '+' : row.type === 'del' ? '-' : ' '} {row.text}
                  </div>
                ))}
              </pre>
            </div>
          )}
        </div>
      )}

      {editing ? (
        <div {...stylex.props(editor.root)}>
          <input
            {...stylex.props(editor.title)}
            value={draftTitle}
            onChange={(e) => setDraftTitle(e.target.value)}
            placeholder="Title"
          />
          <textarea
            {...stylex.props(editor.content)}
            value={draftContent}
            onChange={(e) => setDraftContent(e.target.value)}
            spellCheck={false}
          />
        </div>
      ) : (
        <>
          <h2 {...stylex.props(detailStyles.title)}>{detail.title}</h2>
          {renderArtifactBody(detail, rawId)}
        </>
      )}
    </div>
  );
}

function renderArtifactBody(
  detail: {kind: string; content: string; mimeType: string | null | undefined; title: string},
  rawId: string,
) {
  const src = artifactDownloadUrl(rawId);
  switch (detail.kind) {
    case 'audio':
      return <audio {...stylex.props(detailStyles.media)} controls src={src} />;
    case 'video':
      return <video {...stylex.props(detailStyles.media)} controls src={src} />;
    case 'image':
      return <img {...stylex.props(detailStyles.media)} src={src} alt={detail.title} />;
    case 'markdown':
      return (
        <div
          {...stylex.props(prose.base, detailStyles.content)}
          data-md
          dangerouslySetInnerHTML={{__html: marked.parse(detail.content) as string}}
        />
      );
    default:
      return (
        <div {...stylex.props(prose.base, detailStyles.content)} data-md>
          Binary artifact ({detail.mimeType || 'unknown type'}) — use Download to view it.
        </div>
      );
  }
}
