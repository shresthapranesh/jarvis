import * as stylex from '@stylexjs/stylex';
import {Link, type LinkProps} from '@tanstack/react-router';
import {type ReactNode, useCallback, useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {BoardQuery as TBoardQuery} from '../__generated__/BoardQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {useBoardTaskEvents} from '../hooks/useBoardTaskEvents';
import {usePendingApprovals} from '../hooks/usePendingApprovals';
import {usePollingRefresh} from '../hooks/usePollingRefresh';
import {refreshRunningTasks, useRunningTasks} from '../hooks/useRunningTasks';
import {relativeTime} from '../lib/format';
import {runLink} from '../lib/runLink';
import type {BoardItem, BoardState, BoardTask, PendingApproval, RunningTask} from '../lib/types';
import {commitAnswerBoardTask} from '../relay/AnswerBoardTaskMutation';
import {boardQuery, DONE_PAGE, mapBoardItem, refreshBoard} from '../relay/BoardQuery';
import {commitCreateBoardTask} from '../relay/CreateBoardTaskMutation';
import {commitDecomposeBoardTask} from '../relay/DecomposeBoardTaskMutation';
import {commitDeleteBoardTask} from '../relay/DeleteBoardTaskMutation';
import {commitSetBoardTaskStatus} from '../relay/SetBoardTaskStatusMutation';
import {commitStopBoardTask} from '../relay/StopBoardTaskMutation';
import {commitStopRunningTask} from '../relay/StopRunningTaskMutation';
import {commitUpdateBoardTask} from '../relay/UpdateBoardTaskMutation';
import {kf} from '../theme/keyframes.stylex';
import {ApprovalControls} from './ApprovalControls';
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
import {btn, field, iconBtn, kindBadge, kindBadgeStyle, page, PageHeader, Switch} from './ui';

// What each piece of work is doing — a chat, a board task, an automation's or
// a workflow's run. Todo holds only tasks: nothing else exists before it runs.
const COLUMNS: Array<{key: BoardState; label: string}> = [
  {key: 'todo', label: 'Todo'},
  {key: 'working', label: 'Working'},
  {key: 'needs_me', label: 'Needs me'},
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

/** Where a request came from, when that is a page: its workflow, its conversation. */
function requestLink(a: PendingApproval): LinkProps | null {
  if (a.source === 'workflow' && a.parent_id) {
    return {to: '/workflow/$id', params: {id: a.parent_id}};
  }
  if (a.source === 'automation') return {to: '/automation'};
  if (a.parent_id) return {to: '/c/$id', params: {id: a.parent_id}};
  return null;
}

const RUN_LABEL: Record<string, string> = {
  chat: 'chat',
  automation: 'automation',
  workflow: 'workflow',
};

// The old rules reached this through `.board-col--running .board-col-head
// span:first-child`. The column already knows its key, so the tint is applied
// directly to the title.
function colTitleStyle(key: BoardState) {
  if (key === 'working') return board.colTitleRunning;
  if (key === 'needs_me' || key === 'blocked') return board.colTitleBlocked;
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
  // Done shows its most recent; "Show more" asks for another page of them.
  const [doneLimit, setDoneLimit] = useState(DONE_PAGE);
  const data = useLazyLoadQuery<TBoardQuery>(
    boardQuery,
    {doneLimit},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const items = useMemo(() => data.board.items.map(mapBoardItem), [data.board.items]);
  const doneTotal = data.board.doneTotal;
  const tasks = useMemo(
    () => items.map((i) => i.task).filter((t): t is BoardTask => t !== null),
    [items],
  );

  const refresh = useCallback(() => refreshBoard(doneLimit), [doneLimit]);

  // The runs and the requests the shell already polls. A request joins the
  // card of what asked it — a chat, a task — and one with no card here (a
  // workflow's, an automation's, a deferred action) gets a card of its own.
  const running = useRunningTasks();
  const approvals = usePendingApprovals();
  const {asked, loose, runs} = useMemo(() => {
    const ids = new Set(items.map((i) => i.id));
    const taskIds = new Set(tasks.map((t) => t.id));
    const asked = new Map<string, PendingApproval[]>();
    const loose: PendingApproval[] = [];
    for (const a of approvals) {
      // A task's own question is the answer box on its card.
      if (a.board_task_id && taskIds.has(a.board_task_id)) continue;
      if (!a.deferred && a.parent_id && ids.has(a.parent_id)) {
        asked.set(a.parent_id, [...(asked.get(a.parent_id) ?? []), a]);
      } else {
        loose.push(a);
      }
    }
    // Runs with no card: an automation's, a workflow's, a chat that is not
    // listed (incognito, a bot's). A task's run and a sub-agent's turn are
    // its task's and its parent's.
    const runs = running.filter(
      (r) =>
        !r.done &&
        r.kind !== 'board_task' &&
        (r.kind as string) !== 'subagent' &&
        !(r.parent_id && ids.has(r.parent_id)) &&
        // Stopped on a request shown as a card of its own.
        !(r.has_interrupt && loose.some((a) => a.parent_id && a.parent_id === r.parent_id)),
    );
    return {asked, loose, runs};
  }, [items, tasks, approvals, running]);

  // Cards move on their own — the dispatcher promotes and claims tasks server
  // side — so the board polls regardless of what this user is doing.
  usePollingRefresh(refresh, 3000);

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [deleteTarget, setDeleteTarget] = useState<BoardTask | null>(null);
  // The one column a phone shows; wider screens show them all and ignore it.
  const [phoneCol, setPhoneCol] = useState<BoardState>('todo');

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

  // A chat's turn or a run with no task behind it: stopped by its run id.
  const stopRunAction = useAsyncAction(
    async (runId: string) => {
      await commitStopRunningTask(runId);
      await Promise.all([refreshRunningTasks(), refresh()]);
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

  // Done is counted whole, not by the cards the cut left on screen.
  function countIn(state: BoardState): number {
    return state === 'done' ? doneTotal : columns.get(state)!.length;
  }

  function waitingOn(t: BoardTask): string[] {
    return t.parent_ids
      .map((pid) => byId.get(pid))
      .filter((p): p is BoardTask => Boolean(p) && p!.status !== 'done')
      .map((p) => p.title);
  }

  // A chat: its title is the way into the conversation.
  function renderChat(item: BoardItem, state: BoardState) {
    const requests = asked.get(item.id) ?? [];
    return (
      <li
        key={item.id}
        {...stylex.props(
          card.root,
          state === 'working' && card.running,
          (state === 'needs_me' || state === 'blocked') && card.blocked,
        )}
      >
        <Link to="/c/$id" params={{id: item.id}} {...stylex.props(card.title, card.link)}>
          {item.title || 'Untitled chat'}
        </Link>
        <div {...stylex.props(card.meta)}>
          <span {...stylex.props(chip.base)}>chat</span>
          <span {...stylex.props(card.time)}>{relativeTime(item.last_active_at)}</span>
        </div>
        {requests.length === 0 && item.reason && (
          <p {...stylex.props(card.reason, state === 'needs_me' && card.reasonQuestion)}>
            {item.reason}
          </p>
        )}
        {requests.map(renderRequest)}
        {state === 'working' && item.run_id && (
          <div {...stylex.props(card.actions)}>{stopButton(item.run_id)}</div>
        )}
      </li>
    );
  }

  // A request on the card of what asked it: the question, then the answer.
  function renderRequest(a: PendingApproval) {
    return (
      <div key={a.id}>
        <p {...stylex.props(card.reason, card.reasonQuestion)}>{a.question}</p>
        <ApprovalControls approval={a} onResolved={() => void refresh()} />
      </div>
    );
  }

  function stopButton(runId: string) {
    return (
      <button
        {...stylex.props(iconBtn.base, iconBtn.danger)}
        title="Stop run"
        disabled={stopRunAction.pending}
        onClick={() => void stopRunAction.run(runId)}
      >
        <StopIcon size={13} />
      </button>
    );
  }

  // A request with no card of its own: what asks, the question, the answer.
  function renderLoose(a: PendingApproval) {
    const link = requestLink(a);
    return (
      <li key={a.id} {...stylex.props(card.root, card.blocked)}>
        <div {...stylex.props(card.title)}>{a.label}</div>
        <div {...stylex.props(card.meta)}>
          <span {...stylex.props(kindBadge.base, kindBadgeStyle(a.source))}>{a.source}</span>
          {a.deferred && (
            <span
              {...stylex.props(chip.base)}
              title="Nothing is blocked — approving is what runs it"
            >
              runs on approval
            </span>
          )}
          <span {...stylex.props(card.time)}>{relativeTime(a.requested_at)}</span>
        </div>
        <p {...stylex.props(card.reason, card.reasonQuestion)}>{a.question}</p>
        <ApprovalControls approval={a} onResolved={() => void refresh()} />
        {link && (
          <div {...stylex.props(card.actions)}>
            <Link {...link} {...stylex.props(card.transcript)} title="Open what asked">
              open
            </Link>
          </div>
        )}
      </li>
    );
  }

  // A run with no conversation on the board: an automation's, a workflow's.
  function renderRun(r: RunningTask) {
    const link = runLink(r);
    return (
      <li key={r.id} {...stylex.props(card.root, !r.has_interrupt && card.running)}>
        <div {...stylex.props(card.title)}>{r.label || 'Untitled run'}</div>
        <div {...stylex.props(card.meta)}>
          <span {...stylex.props(kindBadge.base, kindBadgeStyle(r.kind))}>
            {RUN_LABEL[r.kind] ?? r.kind}
          </span>
          <span {...stylex.props(card.time)}>{relativeTime(r.started_at)}</span>
          {r.cancelled && <span {...stylex.props(chip.base)}>stopping…</span>}
        </div>
        <div {...stylex.props(card.actions)}>
          {!r.cancelled && stopButton(r.id)}
          {link && (
            <Link {...link} {...stylex.props(card.transcript)} title="Open the run">
              open
            </Link>
          )}
        </div>
      </li>
    );
  }

  // A request on a card moves it to Needs me, whatever its run is doing.
  function stateOf(item: BoardItem): BoardState {
    return asked.has(item.id) ? 'needs_me' : item.state;
  }

  // Each column's cards: requests with no card of their own lead Needs me,
  // and runs with none follow the conversations in theirs.
  const columns = new Map<BoardState, ReactNode[]>(COLUMNS.map((c) => [c.key, []]));
  for (const a of loose) {
    columns.get('needs_me')!.push(renderLoose(a));
  }
  for (const item of items) {
    const state = stateOf(item);
    columns.get(state)!.push(item.task ? renderCard(item.task, item) : renderChat(item, state));
  }
  for (const r of runs) columns.get(r.has_interrupt ? 'needs_me' : 'working')!.push(renderRun(r));

  function renderCard(t: BoardTask, item: BoardItem) {
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
          {t.status === 'ready' && <span {...stylex.props(chip.base)}>queued</span>}
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
        {(asked.get(item.id) ?? []).map(renderRequest)}
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
              title="Open the task's conversation"
            >
              open
            </Link>
          )}
        </div>
      </li>
    );
  }

  return (
    <div {...stylex.props(board.page)}>
      <PageHeader
        title="Activity"
        subtitle="Everything the agent is doing, and what is waiting on you."
        details={
          <>
            Chats, tasks and the runs of automations and workflows, each in the column of what it is
            doing. <strong>Needs me</strong> holds every question and approval: answer it on its
            card. A task waits in <strong>Todo</strong>: queued ones are picked up automatically,
            and one that depends on others waits until they finish, then gets their summaries as
            context. A paused workflow does not survive a restart; its request is dropped when the
            server starts.
          </>
        }
        actions={
          <button {...stylex.props(btn.base, btn.primary)} onClick={openAdd}>
            <PlusIcon size={14} /> New task
          </button>
        }
      />

      {actionError && !editor && <div {...stylex.props(page.error)}>{actionError}</div>}

      {
        <div {...stylex.props(board.tabs)} role="tablist" aria-label="Board columns">
          {COLUMNS.map((col) => (
            <button
              key={col.key}
              type="button"
              role="tab"
              aria-selected={phoneCol === col.key}
              {...stylex.props(board.tab, phoneCol === col.key && board.tabActive)}
              onClick={() => setPhoneCol(col.key)}
            >
              {col.label}
              <span {...stylex.props(board.tabCount)}>{countIn(col.key)}</span>
            </button>
          ))}
        </div>
      }
      {
        <div {...stylex.props(board.columns)}>
          {COLUMNS.map((col) => {
            const cards = columns.get(col.key)!;
            const hidden = col.key === 'done' ? doneTotal - cards.length : 0;
            return (
              <section
                key={col.key}
                {...stylex.props(board.col, phoneCol !== col.key && board.colOffPhone)}
              >
                <header {...stylex.props(board.colHead)}>
                  <span {...stylex.props(colTitleStyle(col.key))}>{col.label}</span>
                  <span {...stylex.props(board.colCount)}>{countIn(col.key)}</span>
                </header>
                <ul {...stylex.props(board.cards)}>
                  {cards.length > 0 ? (
                    cards
                  ) : (
                    <li {...stylex.props(board.emptyCol)}>Nothing here</li>
                  )}
                  {hidden > 0 && (
                    <li>
                      <button
                        type="button"
                        {...stylex.props(btn.base, board.more)}
                        onClick={() => setDoneLimit(doneLimit + DONE_PAGE)}
                      >
                        Show more ({hidden} older)
                      </button>
                    </li>
                  )}
                </ul>
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
