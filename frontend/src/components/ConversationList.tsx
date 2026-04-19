import {useQuery, useQueryClient} from '@tanstack/react-query';
import {Link, useNavigate, useParams} from '@tanstack/react-router';
import {useState} from 'react';

import {
  deleteConversation,
  fetchConversations,
  formatRelativeTime,
  renameConversation,
} from '../lib/api';

export function ConversationList() {
  const {data: conversations = []} = useQuery({
    queryKey: ['conversations'],
    queryFn: fetchConversations,
    refetchInterval: false,
  });

  const params = useParams({strict: false}) as {id?: string};
  const activeId = params.id;
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const [editingId, setEditingId] = useState<string | null>(null);
  const [editValue, setEditValue] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  function startEdit(id: string, currentTitle: string | null) {
    setEditingId(id);
    setEditValue(currentTitle ?? '');
  }

  async function commitRename(id: string) {
    const title = editValue.trim();
    setEditingId(null);
    if (!title) return;
    await renameConversation(id, title);
    await queryClient.invalidateQueries({queryKey: ['conversations']});
    await queryClient.invalidateQueries({queryKey: ['conversation', id]});
  }

  async function handleDelete(id: string) {
    await deleteConversation(id);
    await queryClient.invalidateQueries({queryKey: ['conversations']});
    if (id === activeId) navigate({to: '/'});
  }

  return (
    <nav className="conv-list">
      <div className="conv-list-header">
        <span className="conv-list-title">Conversations</span>
        <Link to="/" className="new-chat-btn" title="New chat">
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
      <div className="conv-list-body">
        {conversations.length === 0 ? (
          <div className="conv-empty">No conversations yet</div>
        ) : (
          conversations.map((conv) => (
            <div key={conv.id} className={`conv-row${conv.id === activeId ? ' active' : ''}`}>
              {editingId === conv.id ? (
                <input
                  className="conv-title-input"
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
                <Link to="/c/$id" params={{id: conv.id}} className="conv-link">
                  <span className="conv-title">{conv.title ?? 'Untitled'}</span>
                  <span className="conv-meta">{formatRelativeTime(conv.created_at)}</span>
                </Link>
              )}
              <div className="conv-actions">
                {confirmDeleteId === conv.id ? (
                  <>
                    <span className="conv-delete-confirm-label">Delete?</span>
                    <button
                      className="conv-action-btn conv-action-btn--danger"
                      title="Confirm delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        setConfirmDeleteId(null);
                        handleDelete(conv.id);
                      }}
                    >
                      <svg
                        width="11"
                        height="11"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2.5"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >
                        <polyline points="20 6 9 17 4 12" />
                      </svg>
                    </button>
                    <button
                      className="conv-action-btn"
                      title="Cancel"
                      onClick={(e) => {
                        e.stopPropagation();
                        setConfirmDeleteId(null);
                      }}
                    >
                      <svg
                        width="11"
                        height="11"
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
                  </>
                ) : (
                  <>
                    <button
                      className="conv-action-btn"
                      title="Rename"
                      onClick={(e) => {
                        e.stopPropagation();
                        startEdit(conv.id, conv.title);
                      }}
                    >
                      <svg
                        width="11"
                        height="11"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >
                        <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7" />
                        <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z" />
                      </svg>
                    </button>
                    <button
                      className="conv-action-btn conv-action-btn--danger"
                      title="Delete"
                      onClick={(e) => {
                        e.stopPropagation();
                        setConfirmDeleteId(conv.id);
                      }}
                    >
                      <svg
                        width="11"
                        height="11"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >
                        <polyline points="3 6 5 6 21 6" />
                        <path d="M19 6l-1 14a2 2 0 0 1-2 2H8a2 2 0 0 1-2-2L5 6" />
                        <path d="M10 11v6M14 11v6" />
                        <path d="M9 6V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v2" />
                      </svg>
                    </button>
                  </>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </nav>
  );
}
