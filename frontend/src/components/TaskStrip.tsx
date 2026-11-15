import * as stylex from '@stylexjs/stylex';
import {Link} from '@tanstack/react-router';
import {Suspense, useCallback} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ConversationTaskQuery} from '../__generated__/ConversationTaskQuery.graphql';
import {useBoardTaskEvents} from '../hooks/useBoardTaskEvents';
import {usePollingRefresh} from '../hooks/usePollingRefresh';
import {loadConversationPage} from '../relay/ConversationPageQuery';
import {conversationTaskQuery, fetchConversationTask} from '../relay/ConversationTaskQuery';
import {encodeGlobalId} from '../relay/globalId';
import {colors, type} from '../theme/tokens.stylex';
import {card} from './TaskBoard.styles';

/** The board task behind a conversation, as much as its page needs. */
export interface StripTask {
  status: string;
  blockedKind?: string | null;
  blockedReason?: string | null;
  runId?: string | null;
}

const TAIL_CHARS = 280;

/** True while a reply in the conversation is the answer to the task's question. */
export function taskNeedsAnswer(task: StripTask): boolean {
  return task.status === 'blocked' && task.blockedKind === 'needs_input';
}

/** The strip for a task's conversation (`boardtask_<id>`), with its own read. */
export function ConversationTaskStrip({conversationId}: {conversationId: string}) {
  return (
    <Suspense fallback={null}>
      <LoadedStrip conversationId={conversationId} />
    </Suspense>
  );
}

function LoadedStrip({conversationId}: {conversationId: string}) {
  const data = useLazyLoadQuery<ConversationTaskQuery>(
    conversationTaskQuery,
    {id: encodeGlobalId('Conversation', conversationId)},
    {fetchPolicy: 'store-and-network'},
  );
  // The task and the messages its run wrote move together.
  const refresh = useCallback(
    () =>
      Promise.all([fetchConversationTask(conversationId), loadConversationPage(conversationId)]),
    [conversationId],
  );
  const task = data.conversation?.boardTask;
  return task ? <TaskStrip task={task} onChanged={refresh} /> : null;
}

/**
 * Where a task's conversation stands, above its composer: the run's live text
 * while it works, its question while it waits on one, or why it stopped. A
 * task moves on the server's own schedule, so the strip polls `onChanged`
 * while it is queued or running.
 */
function TaskStrip({task, onChanged}: {task: StripTask; onChanged: () => Promise<unknown>}) {
  const moving = task.status === 'ready' || task.status === 'running';
  usePollingRefresh(onChanged, moving ? 3000 : null);
  const running = task.status === 'running';
  const {text, streaming} = useBoardTaskEvents(
    running ? (task.runId ?? null) : null,
    () => void onChanged(),
  );

  if (running) {
    const tail = text.length > TAIL_CHARS ? `…${text.slice(-TAIL_CHARS)}` : text;
    return (
      <div {...stylex.props(strip.root)}>
        <span {...stylex.props(strip.label)}>Task is working</span>
        <p {...stylex.props(card.tail)}>
          {tail || 'working…'}
          {streaming && <span {...stylex.props(card.tailCursor)} aria-hidden="true" />}
        </p>
      </div>
    );
  }
  if (task.status === 'blocked') {
    const question = taskNeedsAnswer(task);
    return (
      <div {...stylex.props(strip.root)}>
        <span {...stylex.props(strip.label, strip.warn)}>
          {question ? 'Task needs your answer — reply below to resume it' : 'Task is blocked'}
        </span>
        {task.blockedReason && (
          <p {...stylex.props(card.reason, question && card.reasonQuestion)}>
            {task.blockedReason}
          </p>
        )}
      </div>
    );
  }
  if (task.status === 'todo' || task.status === 'ready') {
    return (
      <div {...stylex.props(strip.root)}>
        <span {...stylex.props(strip.label)}>
          {task.status === 'ready' ? 'Task is queued to run' : 'Task is parked'} ·{' '}
          <Link to="/activity" {...stylex.props(strip.link)}>
            activity
          </Link>
        </span>
      </div>
    );
  }
  return null;
}

const strip = stylex.create({
  root: {
    // The composer's column, so the strip sits squarely on top of it.
    maxWidth: {default: 740, '@media (max-width: 860px)': 'none'},
    marginInline: 'auto',
    marginBlockEnd: 8,
    paddingBlock: 8,
    paddingInline: 10,
    borderRadius: 3,
    backgroundColor: colors.surface2,
  },
  label: {fontSize: type.tMicro, color: colors.textDim},
  warn: {color: colors.warningText},
  link: {
    color: {default: colors.accent, ':hover': colors.accentStrong},
    textDecorationLine: {default: 'none', ':hover': 'underline'},
  },
});
