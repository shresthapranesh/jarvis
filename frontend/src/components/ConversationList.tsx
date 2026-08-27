import * as stylex from '@stylexjs/stylex';
import {Link, useNavigate, useParams} from '@tanstack/react-router';
import {useEffect, useMemo, useState} from 'react';
import {createPortal} from 'react-dom';
import {useLazyLoadQuery} from 'react-relay';

import type {ConversationListQuery} from '../__generated__/ConversationListQuery.graphql';
import {BUCKET_ORDER, relativeTime, timeBucket, type TimeBucket} from '../lib/format';
import {conversationListQuery, refreshConversationList} from '../relay/ConversationListQuery';
import {commitDeleteConversation} from '../relay/DeleteConversationMutation';
import {decodeGlobalId} from '../relay/globalId';
import {commitUpdateConversation} from '../relay/UpdateConversationMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {list, menu as menuStyles, row} from './ConversationList.styles';
import {useQueryRetry} from './QueryBoundary';

interface MenuAnchor {
  id: string;
  x: number;
  y: number;
}

export function ConversationList() {
  const data = useLazyLoadQuery<ConversationListQuery>(
    conversationListQuery,
    {},
    // fetchKey is what makes the enclosing QueryBoundary's Retry button work —
    // without it a retry re-reads Relay's cached error and throws again.
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );

  const conversations = useMemo(
    () =>
      data.conversations.map((c) => ({
        gid: c.id,
        id: decodeGlobalId(c.id),
        title: c.title,
        pinned: c.pinned,
        createdAt: c.createdAt as string,
      })),
    [data.conversations],
  );

  // Pinned float to their own section; everything else falls into calendar
  // buckets so a long history stays scannable without a search box.
  const sections = useMemo(() => {
    const pinned = conversations.filter((c) => c.pinned);
    const rest = conversations.filter((c) => !c.pinned);

    const byBucket = new Map<TimeBucket, typeof rest>();
    for (const conv of rest) {
      const bucket = timeBucket(conv.createdAt);
      const list = byBucket.get(bucket);
      if (list) list.push(conv);
      else byBucket.set(bucket, [conv]);
    }

    const out: {heading: string; items: typeof rest}[] = [];
    if (pinned.length) out.push({heading: 'Pinned', items: pinned});
    for (const bucket of BUCKET_ORDER) {
      const items = byBucket.get(bucket);
      if (items?.length) out.push({heading: bucket, items});
    }
    return out;
  }, [conversations]);

  const params = useParams({strict: false}) as {id?: string};
  const activeId = params.id;
  const navigate = useNavigate();

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [menu, setMenu] = useState<MenuAnchor | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<{id: string; title: string} | null>(null);

  useEffect(() => {
    if (!menu) return;
    function close() {
      setMenu(null);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setMenu(null);
    }
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', onKey);
    };
  }, [menu]);

  function openMenu(e: React.MouseEvent, id: string) {
    e.preventDefault();
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    setMenu(menu?.id === id ? null : {id, x: rect.right, y: rect.bottom + 4});
  }

  function startEdit(id: string, currentTitle: string | null) {
    setEditingId(id);
    setEditValue(currentTitle ?? '');
  }

  async function commitRename(id: string) {
    const title = editValue.trim();
    setEditingId(null);
    if (!title) return;
    await commitUpdateConversation(id, {title});
    await refreshConversationList();
  }

  async function togglePin(id: string, pinned: boolean) {
    await commitUpdateConversation(id, {pinned: !pinned});
    await refreshConversationList();
  }

  async function handleDelete(id: string) {
    await commitDeleteConversation(id);
    await refreshConversationList();
    if (id === activeId) navigate({to: '/'});
  }

  const menuConv = menu ? conversations.find((c) => c.id === menu.id) : null;

  return (
    <nav {...stylex.props(list.root)}>
      <div {...stylex.props(list.header)}>
        <span {...stylex.props(list.title)}>Conversations</span>
        <Link to="/" {...stylex.props(list.newChat)} title="New chat">
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
            <line x1="12" y1="5" x2="12" y2="19" />
            <line x1="5" y1="12" x2="19" y2="12" />
          </svg>
        </Link>
      </div>
      <div {...stylex.props(list.body)}>
        {conversations.length === 0 ? (
          <p {...stylex.props(list.empty)}>
            No conversations yet. Press <kbd {...stylex.props(list.kbd)}>⌘K</kbd> to start one.
          </p>
        ) : (
          sections.map((section) => (
            <section {...stylex.props(list.section)} key={section.heading}>
              <h3 {...stylex.props(list.heading)}>{section.heading}</h3>
              {section.items.map((conv) => {
                const active = conv.id === activeId;
                const menuOpen = menu?.id === conv.id;
                return (
                  <div
                    key={conv.id}
                    {...stylex.props(row.root, active && row.active, menuOpen && row.menuOpen)}
                  >
                    {editingId === conv.id ? (
                      <input
                        {...stylex.props(row.titleInput)}
                        value={editValue}
                        autoFocus
                        onChange={(e) => setEditValue(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') commitRename(conv.id);
                          if (e.key === 'Escape') setEditingId(null);
                        }}
                        onBlur={() => commitRename(conv.id)}
                      />
                    ) : (
                      <Link to="/c/$id" params={{id: conv.id}} {...stylex.props(row.link)}>
                        <span {...stylex.props(row.title, active && row.titleActive)}>
                          {conv.title ?? 'Untitled'}
                        </span>
                        <span {...stylex.props(row.time)}>{relativeTime(conv.createdAt)}</span>
                      </Link>
                    )}
                    <button
                      {...stylex.props(row.menuBtn, menuOpen && row.menuBtnOpen)}
                      title="More actions"
                      onClick={(e) => openMenu(e, conv.id)}
                      onPointerDown={(e) => e.stopPropagation()}
                    >
                      <svg
                        width="13"
                        height="13"
                        viewBox="0 0 24 24"
                        fill="currentColor"
                        stroke="none"
                      >
                        <circle cx="5" cy="12" r="1.8" />
                        <circle cx="12" cy="12" r="1.8" />
                        <circle cx="19" cy="12" r="1.8" />
                      </svg>
                    </button>
                  </div>
                );
              })}
            </section>
          ))
        )}
      </div>
      {menu &&
        menuConv &&
        createPortal(
          <div
            {...stylex.props(menuStyles.root)}
            style={{top: menu.y, left: menu.x}}
            onPointerDown={(e) => e.stopPropagation()}
          >
            <button
              {...stylex.props(menuStyles.item)}
              onClick={() => {
                setMenu(null);
                togglePin(menuConv.id, menuConv.pinned);
              }}
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                {...stylex.props(menuStyles.icon)}
              >
                <path d="M12 17v5" />
                <path d="M9 10.76a2 2 0 0 1-1.11 1.79l-1.78.9A2 2 0 0 0 5 15.24V16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1v-.76a2 2 0 0 0-1.11-1.79l-1.78-.9A2 2 0 0 1 15 10.76V6h1a2 2 0 0 0 0-4H8a2 2 0 0 0 0 4h1z" />
              </svg>
              {menuConv.pinned ? 'Unpin' : 'Pin'}
            </button>
            <button
              {...stylex.props(menuStyles.item)}
              onClick={() => {
                setMenu(null);
                startEdit(menuConv.id, menuConv.title ?? null);
              }}
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                {...stylex.props(menuStyles.icon)}
              >
                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
              </svg>
              Rename
            </button>
            <button
              {...stylex.props(menuStyles.item, menuStyles.itemDanger)}
              onClick={() => {
                setMenu(null);
                setDeleteTarget({id: menuConv.id, title: menuConv.title ?? 'Untitled'});
              }}
            >
              <svg
                width="12"
                height="12"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                {...stylex.props(menuStyles.icon)}
              >
                <polyline points="3 6 5 6 21 6" />
                <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                <path d="M10 11v6M14 11v6" />
                <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
              </svg>
              Delete
            </button>
          </div>,
          document.body,
        )}
      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete conversation"
        message={
          <>
            Delete <strong>{deleteTarget?.title}</strong>? This removes its messages, artifacts, and
            documents.
          </>
        }
        confirmLabel="Delete"
        danger
        onConfirm={() => {
          if (deleteTarget) handleDelete(deleteTarget.id);
          setDeleteTarget(null);
        }}
        onCancel={() => setDeleteTarget(null)}
      />
    </nav>
  );
}
