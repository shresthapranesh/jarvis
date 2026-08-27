import * as stylex from '@stylexjs/stylex';
import {Link} from '@tanstack/react-router';
import {useCallback, useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {BoardTasksQuery as TBoardTasksQuery} from '../__generated__/BoardTasksQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {useBoardTaskEvents} from '../hooks/useBoardTaskEvents';
import {usePollingRefresh} from '../hooks/usePollingRefresh';
import type {BoardTask, BoardTaskStatus} from '../lib/types';
import {commitAnswerBoardTask} from '../relay/AnswerBoardTaskMutation';
import {boardTasksQuery, mapBoardTask, refreshBoardTasks} from '../relay/BoardTasksQuery';
import {commitCreateBoardTask} from '../relay/CreateBoardTaskMutation';
import {commitDecomposeBoardTask} from '../relay/DecomposeBoardTaskMutation';
import {commitDeleteBoardTask} from '../relay/DeleteBoardTaskMutation';
import {commitSetBoardTaskStatus} from '../relay/SetBoardTaskStatusMutation';
import {commitStopBoardTask} from '../relay/StopBoardTaskMutation';
import {commitUpdateBoardTask} from '../relay/UpdateBoardTaskMutation';
import {kf} from '../theme/keyframes.stylex';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {
  ArchiveIcon,
  EditIcon,
  PauseIcon,
  PlayIcon,
  PlusIcon,
  SplitIcon,
  StopIcon,
  TrashIcon,
} from './icons';
import {useQueryRetry} from './QueryBoundary';
import {
  answer as answerStyles,
  board,
  card,
  chip,
  parentDotStyle,
  parents,
} from './TaskBoard.styles';
import {btn, field, iconBtn, page, Switch} from './ui';

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

const EMPTY_DRAFT: Draft = {
  title: '',
  body: '',
  priority: 0,
  start: true,
  decompose: false,
  parentIds: [],
};

type Editor = {mode: 'add'} | {mode: 'edit'; task: BoardTask};

const TAIL_CHARS = 280;

// The old rules reached this through `.board-col--running .board-col-head
// span:first-child`. The column already knows its key, so the tint is applied
// directly to the title.
function colTitleStyle(key: BoardTaskStatus) {
  if (key === 'running') return board.colTitleRunning;
  if (key === 'blocked') return board.colTitleBlocked;
  if (key === 'done') return board.colTitleDone;
  return null;
}

/** Live token tail for a running card — one subscription per running task. */
function RunTail({runId, onFinished}: {runId: string | null; onFinished: () => void}) {
  const {text, streaming, error} = useBoardTaskEvents(runId, onFinished);
  if (error) return <p {...stylex.props(card.reason)}>{error}</p>;
  if (!streaming && !text) return null;
  const tail = text.length > TAIL_CHARS ? `…${text.slice(-TAIL_CHARS)}` : text;
  return (
    <p {...stylex.props(card.tail)}>
      {tail || 'working…'}
      {streaming && <span {...stylex.props(card.tailCursor)} aria-hidden="true" />}
    </p>
  );
}

/** Answer box shown on blocked cards whose agent asked for input. */
function AnswerBox({taskId, onAnswered}: {taskId: string; onAnswered: () => void}) {
  const [answer, setAnswer] = useState('');
  const answerAction = useAsyncAction(() => commitAnswerBoardTask(taskId, answer.trim()), {
    onSuccess: () => {
      setAnswer('');
      onAnswered();
    },
  });
  return (
    <div {...stylex.props(answerStyles.root)}>
      <textarea
        {...stylex.props(answerStyles.input)}
        value={answer}
        onChange={(e) => setAnswer(e.target.value)}
        rows={2}
        placeholder="Answer the question to resume…"
      />
      <button
        {...stylex.props(btn.base, btn.primary, answerStyles.btn)}
        disabled={!answer.trim() || answerAction.pending}
        onClick={() => void answerAction.run()}
      >
        {answerAction.pending ? 'Resuming…' : 'Answer & resume'}
      </button>
      {answerAction.error && (
        <span {...stylex.props(card.reason)}>{answerAction.error.message}</span>
      )}
    </div>
  );
}

export function TaskBoard() {
  const data = useLazyLoadQuery<TBoardTasksQuery>(
    boardTasksQuery,
    {includeArchived: false},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const tasks = useMemo(() => data.boardTasks.map(mapBoardTask), [data.boardTasks]);

  const refresh = useCallback(() => refreshBoardTasks(false), []);

  // Cards move on their own — the dispatcher promotes and claims tasks server
  // side — so the board polls regardless of what this user is doing.
  usePollingRefresh(refresh, 3000);

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [deleteTarget, setDeleteTarget] = useState<BoardTask | null>(null);

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

  const createAction = useAsyncAction(
    async () => {
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
      await refresh();
    },
    {
      onSuccess: closeEditor,
      onError: (e) => {
        // Create may have succeeded with only the decompose step failing —
        // refresh so the parked task is visible next to the error.
        void refresh();
        setActionError(e.message);
      },
    },
  );

  // react-query exposed the in-flight mutation's argument; without it the
  // per-card spinner needs the id tracked explicitly.
  const [decomposingId, setDecomposingId] = useState<string | null>(null);
  const decomposeAction = useAsyncAction(
    async (id: string) => {
      setDecomposingId(id);
      try {
        await commitDecomposeBoardTask(id);
        await refresh();
      } finally {
        setDecomposingId(null);
      }
    },
    {onError: (e) => setActionError(e.message)},
  );

  const updateAction = useAsyncAction(
    async (id: string) => {
      await commitUpdateBoardTask(id, {
        title: draft.title.trim(),
        body: draft.body.trim() || undefined,
        priority: draft.priority,
        parentIds: draft.parentIds,
      });
      await refresh();
    },
    {onSuccess: closeEditor, onError: (e) => setActionError(e.message)},
  );

  const moveAction = useAsyncAction(
    async (id: string, status: 'todo' | 'ready' | 'done' | 'archived') => {
      await commitSetBoardTaskStatus(id, status);
      await refresh();
    },
    {onError: (e) => setActionError(e.message)},
  );

  const stopAction = useAsyncAction(
    async (id: string) => {
      await commitStopBoardTask(id);
      await refresh();
    },
    {onError: (e) => setActionError(e.message)},
  );

  const deleteAction = useAsyncAction(
    async (id: string) => {
      await commitDeleteBoardTask(id);
      await refresh();
    },
    {
      onSuccess: () => setDeleteTarget(null),
      onError: (e) => {
        setDeleteTarget(null);
        setActionError(e.message);
      },
    },
  );

  function submitEditor() {
    if (!editor) return;
    if (editor.mode === 'add') void createAction.run();
    else void updateAction.run(editor.task.id);
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
      <li
        key={t.id}
        {...stylex.props(
          card.root,
          t.status === 'running' && card.running,
          t.status === 'blocked' && card.blocked,
        )}
      >
        <div {...stylex.props(card.title)}>{t.title}</div>
        <div {...stylex.props(card.meta)}>
          {t.priority !== 0 && (
            <span {...stylex.props(chip.base, chip.priority)}>P{t.priority}</span>
          )}
          {t.created_by === 'agent' && <span {...stylex.props(chip.base)}>agent</span>}
          {t.skill && <span {...stylex.props(chip.base)}>skill: {t.skill}</span>}
          {t.failure_count > 0 && (
            <span {...stylex.props(chip.base, chip.danger)}>
              {t.failure_count} failure{t.failure_count > 1 ? 's' : ''}
            </span>
          )}
          {t.status === 'blocked' && t.blocked_kind === 'needs_input' && (
            <span {...stylex.props(chip.base, chip.question)}>needs input</span>
          )}
          {pendingParents.length > 0 && (
            <span {...stylex.props(chip.base)} title={pendingParents.join(', ')}>
              waits on {pendingParents.length}
            </span>
          )}
        </div>
        {t.body && <p {...stylex.props(card.body)}>{t.body}</p>}
        {t.status === 'running' && <RunTail runId={t.run_id} onFinished={refresh} />}
        {t.status === 'blocked' && t.blocked_reason && (
          <p
            {...stylex.props(card.reason, t.blocked_kind === 'needs_input' && card.reasonQuestion)}
          >
            {t.blocked_reason}
          </p>
        )}
        {t.status === 'blocked' && t.blocked_kind === 'needs_input' && (
          <AnswerBox taskId={t.id} onAnswered={refresh} />
        )}
        {t.status === 'done' && t.summary && <p {...stylex.props(card.summary)}>{t.summary}</p>}
        <div {...stylex.props(card.actions)}>
          {(t.status === 'todo' || t.status === 'blocked') && (
            <button
              {...stylex.props(iconBtn.base)}
              title={t.status === 'blocked' ? 'Unblock and queue' : 'Queue for dispatch'}
              onClick={() => void moveAction.run(t.id, 'ready')}
            >
              <PlayIcon size={13} />
            </button>
          )}
          {(t.status === 'todo' || t.status === 'ready') && t.parent_ids.length === 0 && (
            <button
              {...stylex.props(iconBtn.base)}
              title="Split into subtasks with a planner LLM"
              disabled={decomposingId === t.id}
              onClick={() => void decomposeAction.run(t.id)}
            >
              <SplitIcon size={13} style={decomposingId === t.id ? spinner.busy : null} />
            </button>
          )}
          {t.status === 'done' && (
            <button
              {...stylex.props(iconBtn.base)}
              title="Re-run"
              onClick={() => void moveAction.run(t.id, 'ready')}
            >
              <PlayIcon size={13} />
            </button>
          )}
          {t.status === 'ready' && (
            <button
              {...stylex.props(iconBtn.base)}
              title="Park in todo"
              onClick={() => void moveAction.run(t.id, 'todo')}
            >
              <PauseIcon size={13} />
            </button>
          )}
          {t.status === 'running' && (
            <button
              {...stylex.props(iconBtn.base, iconBtn.danger)}
              title="Stop run"
              onClick={() => void stopAction.run(t.id)}
            >
              <StopIcon size={13} />
            </button>
          )}
          {t.status !== 'running' && (
            <button {...stylex.props(iconBtn.base)} title="Edit task" onClick={() => openEdit(t)}>
              <EditIcon size={13} />
            </button>
          )}
          {(t.status === 'done' || t.status === 'blocked') && (
            <button
              {...stylex.props(iconBtn.base)}
              title="Archive"
              onClick={() => void moveAction.run(t.id, 'archived')}
            >
              <ArchiveIcon size={13} />
            </button>
          )}
          {t.status !== 'running' && (
            <button
              {...stylex.props(iconBtn.base, iconBtn.danger)}
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
              {...stylex.props(card.transcript)}
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
    <div {...stylex.props(board.page)}>
      <header {...stylex.props(page.header)}>
        <div {...stylex.props(page.headerMain)}>
          <h1 {...stylex.props(page.title)}>Board</h1>
          <p {...stylex.props(page.subtitle)}>
            Durable background tasks the agent works through on its own. Cards in{' '}
            <strong>ready</strong> are picked up automatically; dependent tasks wait until their
            parents finish and receive their summaries as context. The agent can queue work here too
            via <code>create_task(…)</code>.
          </p>
        </div>
        <div {...stylex.props(page.headerActions)}>
          <button {...stylex.props(btn.base, btn.primary)} onClick={openAdd}>
            <PlusIcon size={14} /> New task
          </button>
        </div>
      </header>

      {actionError && !editor && <div {...stylex.props(page.error)}>{actionError}</div>}

      {
        <div {...stylex.props(board.columns)}>
          {COLUMNS.map((col) => {
            const cards = all.filter((t) => t.status === col.key);
            return (
              <section key={col.key} {...stylex.props(board.col)}>
                <header {...stylex.props(board.colHead)}>
                  <span {...stylex.props(colTitleStyle(col.key))}>{col.label}</span>
                  <span {...stylex.props(board.colCount)}>{cards.length}</span>
                </header>
                <ul {...stylex.props(board.cards)}>{cards.map(renderCard)}</ul>
              </section>
            );
          })}
        </div>
      }

      <FormModal
        open={editor !== null}
        title={editor?.mode === 'edit' ? 'Edit task' : 'New task'}
        subtitle="The body is the instruction the agent follows when the task runs."
        submitLabel={editor?.mode === 'edit' ? 'Save changes' : 'Create task'}
        submitDisabled={!draft.title.trim()}
        pending={createAction.pending || updateAction.pending}
        error={actionError}
        footerExtra={
          editor?.mode === 'add' ? (
            <>
              <Switch
                checked={draft.start}
                disabled={draft.decompose}
                onChange={(next) => setDraft({...draft, start: next})}
                label="Start immediately"
              />
              {draft.parentIds.length === 0 && (
                <Switch
                  checked={draft.decompose}
                  onChange={(next) => setDraft({...draft, decompose: next})}
                  title="A planner LLM splits this into parallel subtasks; the task itself runs last with their results"
                  label="Auto-split into subtasks"
                />
              )}
            </>
          ) : undefined
        }
        onSubmit={submitEditor}
        onClose={closeEditor}
      >
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Title</span>
          <input
            {...stylex.props(field.input)}
            value={draft.title}
            onChange={(e) => setDraft({...draft, title: e.target.value})}
            autoFocus={editor?.mode === 'add'}
            placeholder="Summarize this week's AI research papers"
          />
        </div>
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Instructions</span>
          <textarea
            {...stylex.props(field.textarea)}
            value={draft.body}
            onChange={(e) => setDraft({...draft, body: e.target.value})}
            rows={6}
            placeholder="What should the agent do, and what does 'done' look like?"
          />
        </div>
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Priority (higher runs first)</span>
          <input
            {...stylex.props(field.input, parents.priorityInput)}
            type="number"
            value={draft.priority}
            onChange={(e) => setDraft({...draft, priority: Number(e.target.value) || 0})}
          />
        </div>
        {(() => {
          const editingId = editor?.mode === 'edit' ? editor.task.id : null;
          const candidates = all.filter((t) => t.id !== editingId && t.status !== 'archived');
          if (candidates.length === 0) return null;
          return (
            <div {...stylex.props(field.group)}>
              <span {...stylex.props(field.label)}>
                Depends on — runs after these finish, receiving their summaries
              </span>
              <ul {...stylex.props(parents.list)}>
                {candidates.map((t) => (
                  <li key={t.id}>
                    <label {...stylex.props(parents.option)}>
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
                      <span {...stylex.props(parents.dot, parentDotStyle(t.status))} />
                      {t.title}
                    </label>
                  </li>
                ))}
              </ul>
              {draft.parentIds.length > 0 && editor?.mode === 'add' && (
                <span {...stylex.props(field.hint)}>
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
            Delete <strong>{deleteTarget?.title}</strong>? Its run transcript is deleted with it.
          </p>
        }
        confirmLabel="Delete"
        danger
        onConfirm={() => deleteTarget && void deleteAction.run(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

// The one icon on this page that animates — everything else here is static.
const spinner = stylex.create({
  busy: {
    animationName: kf.spin,
    animationDuration: '1s',
    animationTimingFunction: 'linear',
    animationIterationCount: 'infinite',
  },
});
