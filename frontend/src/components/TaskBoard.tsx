import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {Link} from '@tanstack/react-router';
import {useState} from 'react';

import {useBoardTaskEvents} from '../hooks/useBoardTaskEvents';
import {commitAnswerBoardTask} from '../relay/AnswerBoardTaskMutation';
import {fetchBoardTasks} from '../relay/BoardTasksQuery';
import {commitCreateBoardTask} from '../relay/CreateBoardTaskMutation';
import {commitDecomposeBoardTask} from '../relay/DecomposeBoardTaskMutation';
import {commitDeleteBoardTask} from '../relay/DeleteBoardTaskMutation';
import {commitSetBoardTaskStatus} from '../relay/SetBoardTaskStatusMutation';
import {commitStopBoardTask} from '../relay/StopBoardTaskMutation';
import {commitUpdateBoardTask} from '../relay/UpdateBoardTaskMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {ArchiveIcon, EditIcon, PauseIcon, PlayIcon, PlusIcon, SplitIcon, StopIcon, TrashIcon} from './icons';
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
  decompose: boolean;
  parentIds: string[];
}

const EMPTY_DRAFT: Draft = {title: '', body: '', priority: 0, start: true, decompose: false, parentIds: []};

type Editor = {mode: 'add'} | {mode: 'edit'; task: BoardTask};

const TAIL_CHARS = 280;

/** Live token tail for a running card — one subscription per running task. */
function RunTail({runId, onFinished}: {runId: string | null; onFinished: () => void}) {
  const {text, streaming, error} = useBoardTaskEvents(runId, onFinished);
  if (error) return <p className="board-card-reason">{error}</p>;
  if (!streaming && !text) return null;
  const tail = text.length > TAIL_CHARS ? `…${text.slice(-TAIL_CHARS)}` : text;
  return (
    <p className="board-card-tail">
      {tail || 'working…'}
      {streaming && <span className="board-tail-cursor" aria-hidden="true" />}
    </p>
  );
}

/** Answer box shown on blocked cards whose agent asked for input. */
function AnswerBox({taskId, onAnswered}: {taskId: string; onAnswered: () => void}) {
  const [answer, setAnswer] = useState('');
  const answerMutation = useMutation({
    mutationFn: () => commitAnswerBoardTask(taskId, answer.trim()),
    onSuccess: () => {
      setAnswer('');
      onAnswered();
    },
  });
  return (
    <div className="board-answer">
      <textarea
        className="board-answer-input"
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        rows={2}
        placeholder="Answer the question to resume…"
      />
      <button
        className="artifact-btn primary board-answer-btn"
        disabled={!answer.trim() || answerMutation.isPending}
        onClick={() => answerMutation.mutate()}
      >
        {answerMutation.isPending ? 'Resuming…' : 'Answer & resume'}
      </button>
      {answerMutation.error && (
        <span className="board-card-reason">{(answerMutation.error as Error).message}</span>
      )}
    </div>
  );
}

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
    setDraft({
      title: t.title,
      body: t.body ?? '',
      priority: t.priority,
      start: true,
      decompose: false,
      parentIds: [...t.parent_ids],
    });
    setActionError(null);
    setEditor({mode: 'edit', task: t});
  }

  const createMutation = useMutation({
    mutationFn: async () => {
      const decompose = draft.decompose && draft.parentIds.length === 0;
      const task = await commitCreateBoardTask({
        title: draft.title.trim(),
        body: draft.body.trim() || undefined,
        priority: draft.priority,
        parentIds: draft.parentIds,
        // A task about to be decomposed is parked; its subtasks start instead.
        start: decompose ? false : draft.start,
      });
      if (decompose) await commitDecomposeBoardTask(task.id);
      return task;
    },
    onSuccess: async () => {
      await invalidate();
      closeEditor();
    },
    onError: async (e: Error) => {
      // Create may have succeeded with only the decompose step failing —
      // refresh so the parked task is visible next to the error.
      await invalidate();
      setActionError(e.message);
    },
  });

  const decomposeMutation = useMutation({
    mutationFn: (id: string) => commitDecomposeBoardTask(id),
    onSuccess: () => invalidate(),
    onError: (e: Error) => setActionError(e.message),
  });

  const updateMutation = useMutation({
    mutationFn: ({id}: {id: string}) =>
      commitUpdateBoardTask(id, {
        title: draft.title.trim(),
        body: draft.body.trim() || undefined,
        priority: draft.priority,
        parentIds: draft.parentIds,
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
          {t.status === 'blocked' && t.blocked_kind === 'needs_input' && (
            <span className="board-chip board-chip--question">needs input</span>
          )}
          {pendingParents.length > 0 && (
            <span className="board-chip" title={pendingParents.join(', ')}>
              waits on {pendingParents.length}
            </span>
          )}
        </div>
        {t.body && <p className="board-card-body">{t.body}</p>}
        {t.status === 'running' && <RunTail runId={t.run_id} onFinished={invalidate} />}
        {t.status === 'blocked' && t.blocked_reason && (
          <p className={`board-card-reason${t.blocked_kind === 'needs_input' ? ' board-card-reason--question' : ''}`}>
            {t.blocked_reason}
          </p>
        )}
        {t.status === 'blocked' && t.blocked_kind === 'needs_input' && (
          <AnswerBox taskId={t.id} onAnswered={invalidate} />
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
          {(t.status === 'todo' || t.status === 'ready') && t.parent_ids.length === 0 && (
            <button
              className="icon-btn"
              title="Split into subtasks with a planner LLM"
              disabled={decomposeMutation.isPending && decomposeMutation.variables === t.id}
              onClick={() => decomposeMutation.mutate(t.id)}
            >
              <SplitIcon size={13} className={decomposeMutation.isPending && decomposeMutation.variables === t.id ? 'board-split-busy' : undefined} />
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
            <>
              <label className="switch switch--labeled">
                <input
                  type="checkbox"
                  checked={draft.start}
                  disabled={draft.decompose}
                  onChange={(e) => setDraft({...draft, start: e.target.checked})}
                />
                <span className="switch-track" aria-hidden="true" />
                Start immediately
              </label>
              {draft.parentIds.length === 0 && (
                <label
                  className="switch switch--labeled"
                  title="A planner LLM splits this into parallel subtasks; the task itself runs last with their results"
                >
                  <input
                    type="checkbox"
                    checked={draft.decompose}
                    onChange={(e) => setDraft({...draft, decompose: e.target.checked})}
                  />
                  <span className="switch-track" aria-hidden="true" />
                  Auto-split into subtasks
                </label>
              )}
            </>
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
        {(() => {
          const editingId = editor?.mode === 'edit' ? editor.task.id : null;
          const candidates = all.filter(
            (t) => t.id !== editingId && t.status !== 'archived',
          );
          if (candidates.length === 0) return null;
          return (
            <div className="auto-form-group">
              <span className="auto-form-label">
                Depends on — runs after these finish, receiving their summaries
              </span>
              <ul className="board-parent-picker">
                {candidates.map((t) => (
                  <li key={t.id}>
                    <label className="board-parent-option">
                      <input
                        type="checkbox"
                        checked={draft.parentIds.includes(t.id)}
                        onChange={(e) =>
                          setDraft({
                            ...draft,
                            parentIds: e.target.checked
                              ? [...draft.parentIds, t.id]
                              : draft.parentIds.filter((id) => id !== t.id),
                          })
                        }
                      />
                      <span className={`board-parent-status board-parent-status--${t.status}`} />
                      {t.title}
                    </label>
                  </li>
                ))}
              </ul>
              {draft.parentIds.length > 0 && editor?.mode === 'add' && (
                <span className="auto-form-hint">
                  Dependent tasks wait in todo until every parent is done.
                </span>
              )}
            </div>
          );
        })()}
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
