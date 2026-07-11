import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {Link} from '@tanstack/react-router';
import {useState} from 'react';

import {fetchBoardTasks} from '../relay/BoardTasksQuery';
import {commitCreateBoardTask} from '../relay/CreateBoardTaskMutation';
import {commitDeleteBoardTask} from '../relay/DeleteBoardTaskMutation';
import {commitSetBoardTaskStatus} from '../relay/SetBoardTaskStatusMutation';
import {commitStopBoardTask} from '../relay/StopBoardTaskMutation';
import {commitUpdateBoardTask} from '../relay/UpdateBoardTaskMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {ArchiveIcon, EditIcon, PauseIcon, PlayIcon, PlusIcon, StopIcon, TrashIcon} from './icons';
import type {BoardTask, BoardTaskStatus} from '../lib/types';

const COLUMNS: Array<{key: BoardTaskStatus; label: string}> = [
  {key: 'todo', label: 'Todo'},
  {key: 'ready', label: 'Ready'},
  {key: 'running', label: 'Running'},
  {key: 'blocked', label: 'Blocked'},
  {key: 'done', label: 'Done'},
];

interface Draft {
  title: string;
  body: string;
  priority: number;
  start: boolean;
}

const EMPTY_DRAFT: Draft = {title: '', body: '', priority: 0, start: true};

type Editor = {mode: 'add'} | {mode: 'edit'; task: BoardTask};

export function TaskBoard() {
  const queryClient = useQueryClient();

  const {data: tasks, isLoading, error} = useQuery<BoardTask[]>({
    queryKey: ['board-tasks'],
    queryFn: () => fetchBoardTasks(false),
    refetchInterval: 3000,
  });

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [deleteTarget, setDeleteTarget] = useState<BoardTask | null>(null);

  const invalidate = () => queryClient.invalidateQueries({queryKey: ['board-tasks']});

  function closeEditor() {
    setEditor(null);
    setActionError(null);
  }

  function openAdd() {
    setDraft(EMPTY_DRAFT);
    setActionError(null);
    setEditor({mode: 'add'});
  }

  function openEdit(t: BoardTask) {
    setDraft({title: t.title, body: t.body ?? '', priority: t.priority, start: true});
    setActionError(null);
    setEditor({mode: 'edit', task: t});
  }

  const createMutation = useMutation({
    mutationFn: () =>
      commitCreateBoardTask({
        title: draft.title.trim(),
        body: draft.body.trim() || undefined,
        priority: draft.priority,
        start: draft.start,
      }),
    onSuccess: async () => {
      await invalidate();
      closeEditor();
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const updateMutation = useMutation({
    mutationFn: ({id}: {id: string}) =>
      commitUpdateBoardTask(id, {
        title: draft.title.trim(),
        body: draft.body.trim() || undefined,
        priority: draft.priority,
      }),
    onSuccess: async () => {
      await invalidate();
      closeEditor();
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const moveMutation = useMutation({
    mutationFn: ({id, status}: {id: string; status: 'todo' | 'ready' | 'done' | 'archived'}) =>
      commitSetBoardTaskStatus(id, status),
    onSuccess: () => invalidate(),
    onError: (e: Error) => setActionError(e.message),
  });

  const stopMutation = useMutation({
    mutationFn: (id: string) => commitStopBoardTask(id),
    onSuccess: () => invalidate(),
    onError: (e: Error) => setActionError(e.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => commitDeleteBoardTask(id),
    onSuccess: async () => {
      await invalidate();
      setDeleteTarget(null);
    },
    onError: (e: Error) => {
      setDeleteTarget(null);
      setActionError(e.message);
    },
  });

  function submitEditor() {
    if (!editor) return;
    if (editor.mode === 'add') createMutation.mutate();
    else updateMutation.mutate({id: editor.task.id});
  }

  const all = tasks ?? [];
  const byId = new Map(all.map((t) => [t.id, t]));

  function waitingOn(t: BoardTask): string[] {
    return t.parent_ids
      .map((pid) => byId.get(pid))
      .filter((p): p is BoardTask => Boolean(p) && p!.status !== 'done')
      .map((p) => p.title);
  }

  function renderCard(t: BoardTask) {
    const pendingParents = t.status === 'todo' ? waitingOn(t) : [];
    return (
      <li key={t.id} className={`board-card board-card--${t.status}`}>
        <div className="board-card-title">{t.title}</div>
        <div className="board-card-meta">
          {t.priority !== 0 && <span className="board-chip board-chip--priority">P{t.priority}</span>}
          {t.created_by === 'agent' && <span className="board-chip">agent</span>}
          {t.skill && <span className="board-chip">skill: {t.skill}</span>}
          {t.failure_count > 0 && (
            <span className="board-chip board-chip--danger">{t.failure_count} failure{t.failure_count > 1 ? 's' : ''}</span>
          )}
          {pendingParents.length > 0 && (
            <span className="board-chip" title={pendingParents.join(', ')}>
              waits on {pendingParents.length}
            </span>
          )}
        </div>
        {t.body && <p className="board-card-body">{t.body}</p>}
        {t.status === 'blocked' && t.blocked_reason && (
          <p className="board-card-reason">{t.blocked_reason}</p>
        )}
        {t.status === 'done' && t.summary && (
          <p className="board-card-summary">{t.summary}</p>
        )}
        <div className="board-card-actions">
          {(t.status === 'todo' || t.status === 'blocked') && (
            <button
              className="icon-btn"
              title={t.status === 'blocked' ? 'Unblock and queue' : 'Queue for dispatch'}
              onClick={() => moveMutation.mutate({id: t.id, status: 'ready'})}
            >
              <PlayIcon size={13} />
            </button>
          )}
          {t.status === 'done' && (
            <button
              className="icon-btn"
              title="Re-run"
              onClick={() => moveMutation.mutate({id: t.id, status: 'ready'})}
            >
              <PlayIcon size={13} />
            </button>
          )}
          {t.status === 'ready' && (
            <button
              className="icon-btn"
              title="Park in todo"
              onClick={() => moveMutation.mutate({id: t.id, status: 'todo'})}
            >
              <PauseIcon size={13} />
            </button>
          )}
          {t.status === 'running' && (
            <button
              className="icon-btn icon-btn--danger"
              title="Stop run"
              onClick={() => stopMutation.mutate(t.id)}
            >
              <StopIcon size={13} />
            </button>
          )}
          {t.status !== 'running' && (
            <button className="icon-btn" title="Edit task" onClick={() => openEdit(t)}>
              <EditIcon size={13} />
            </button>
          )}
          {(t.status === 'done' || t.status === 'blocked') && (
            <button
              className="icon-btn"
              title="Archive"
              onClick={() => moveMutation.mutate({id: t.id, status: 'archived'})}
            >
              <ArchiveIcon size={13} />
            </button>
          )}
          {t.status !== 'running' && (
            <button
              className="icon-btn icon-btn--danger"
              title="Delete task"
              onClick={() => setDeleteTarget(t)}
            >
              <TrashIcon size={13} />
            </button>
          )}
          {t.started_at && (
            <Link
              to="/c/$id"
              params={{id: t.conversation_id}}
              className="board-card-transcript"
              title="Open run transcript"
            >
              transcript
            </Link>
          )}
        </div>
      </li>
    );
  }

  return (
    <div className="page board-page">
      <header className="memory-header">
        <div>
          <h1>Board</h1>
          <p className="memory-subtitle">
            Durable background tasks the agent works through on its own. Cards in{' '}
            <strong>ready</strong> are picked up automatically; dependent tasks wait until
            their parents finish and receive their summaries as context. The agent can queue
            work here too via <code>create_task(…)</code>.
          </p>
        </div>
        <div className="memory-header-actions">
          <button className="artifact-btn primary" onClick={openAdd}>
            <PlusIcon size={14} /> New task
          </button>
        </div>
      </header>

      {actionError && !editor && <div className="memory-error">{actionError}</div>}

      {isLoading ? (
        <div className="memory-empty">Loading…</div>
      ) : error ? (
        <div className="memory-empty">Failed to load board: {(error as Error).message}</div>
      ) : (
        <div className="board-columns">
          {COLUMNS.map((col) => {
            const cards = all.filter((t) => t.status === col.key);
            return (
              <section key={col.key} className={`board-col board-col--${col.key}`}>
                <header className="board-col-head">
                  <span>{col.label}</span>
                  <span className="board-col-count">{cards.length}</span>
                </header>
                <ul className="board-cards">{cards.map(renderCard)}</ul>
              </section>
            );
          })}
        </div>
      )}

      <FormModal
        open={editor !== null}
        title={editor?.mode === 'edit' ? 'Edit task' : 'New task'}
        subtitle="The body is the instruction the agent follows when the task runs."
        submitLabel={editor?.mode === 'edit' ? 'Save changes' : 'Create task'}
        submitDisabled={!draft.title.trim()}
        pending={createMutation.isPending || updateMutation.isPending}
        error={actionError}
        footerExtra={
          editor?.mode === 'add' ? (
            <label className="switch switch--labeled">
              <input
                type="checkbox"
                checked={draft.start}
                onChange={(e) => setDraft({...draft, start: e.target.checked})}
              />
              <span className="switch-track" aria-hidden="true" />
              Start immediately
            </label>
          ) : undefined
        }
        onSubmit={submitEditor}
        onClose={closeEditor}
      >
        <div className="auto-form-group">
          <span className="auto-form-label">Title</span>
          <input
            className="auto-form-input"
            value={draft.title}
            onChange={(e) => setDraft({...draft, title: e.target.value})}
            autoFocus={editor?.mode === 'add'}
            placeholder="Summarize this week's AI research papers"
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Instructions</span>
          <textarea
            className="auto-form-textarea"
            value={draft.body}
            onChange={(e) => setDraft({...draft, body: e.target.value})}
            rows={6}
            placeholder="What should the agent do, and what does 'done' look like?"
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Priority (higher runs first)</span>
          <input
            className="auto-form-input board-priority-input"
            type="number"
            value={draft.priority}
            onChange={(e) => setDraft({...draft, priority: Number(e.target.value) || 0})}
          />
        </div>
      </FormModal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete task"
        message={
          <p>
            Delete <strong>{deleteTarget?.title}</strong>? Its run transcript is deleted
            with it.
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
