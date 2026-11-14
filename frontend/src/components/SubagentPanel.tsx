import * as stylex from '@stylexjs/stylex';
import {Link} from '@tanstack/react-router';
import {Suspense, useEffect, useLayoutEffect, useMemo, useRef, useState} from 'react';
import {useLazyLoadQuery, usePaginationFragment} from 'react-relay';

import type {ConversationPageFragment$key} from '../__generated__/ConversationPageFragment.graphql';
import type {ConversationPageQuery as TConversationPageQuery} from '../__generated__/ConversationPageQuery.graphql';
import type {ConversationPageRefetchQuery} from '../__generated__/ConversationPageRefetchQuery.graphql';
import {useTaskEvents} from '../hooks/useTaskEvents';
import {useToast} from '../lib/toast';
import type {Message} from '../lib/types';
import {mapMessage} from '../lib/types';
import type {UploadedFile} from '../lib/uploads';
import {conversationPageFragment} from '../relay/ConversationPageFragment';
import {
  CONVERSATION_PAGE_SIZE,
  conversationPageQuery,
  loadConversationPage,
} from '../relay/ConversationPageQuery';
import {encodeGlobalId} from '../relay/globalId';
import {commitQueueMessage} from '../relay/QueueMessageMutation';
import {commitStartTask} from '../relay/StartTaskMutation';
import {commitStopTask} from '../relay/StopTaskMutation';
import {commitUnqueueMessage} from '../relay/UnqueueMessageMutation';
import {kf} from '../theme/keyframes.stylex';
import {channels, colors, css, type} from '../theme/tokens.stylex';
import {InputBox} from './InputBox';
import {InterruptPrompt} from './InterruptPrompt';
import {MessageBubble, StreamingBubble} from './MessageBubble';
import {QueuedMessages} from './QueuedMessages';
import {SUBAGENT_DOT, SUBAGENT_STATUS, type SubagentInfo} from './SubagentChip';
import {closeBtn, errorBubble, ResizeHandle, turn, worker} from './ui';

interface Props {
  subagent: SubagentInfo;
  width: number;
  onResize: (width: number) => void;
  onClose: () => void;
}

/**
 * A sub-agent's conversation, open beside the one whose agent spawned it.
 *
 * It is the sub-agent's real conversation rather than a summary of it: the
 * same messages its own page shows, streaming live while it works, with a
 * composer that writes to it directly.
 */
export function SubagentPanel({subagent, width, onResize, onClose}: Props) {
  return (
    <aside {...stylex.props(styles.root)} style={{width}} aria-label="Sub-agent conversation">
      <ResizeHandle width={width} onResize={onResize} label="Resize sub-agent panel" />
      <header {...stylex.props(styles.header)}>
        <span
          {...stylex.props(worker.dot, worker[SUBAGENT_DOT[subagent.status] ?? 'dotUnknown'])}
        />
        <span {...stylex.props(styles.title)} title={subagent.title ?? undefined}>
          {subagent.title ?? 'Untitled task'}
        </span>
        <span {...stylex.props(styles.status)}>
          {SUBAGENT_STATUS[subagent.status] ?? subagent.status}
        </span>
        <Link
          to="/c/$id"
          params={{id: subagent.conversationId}}
          search={{}}
          {...stylex.props(closeBtn.base)}
          title="Open as its own page"
          aria-label="Open as its own page"
        >
          <svg
            width="13"
            height="13"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.2"
            strokeLinecap="round"
            strokeLinejoin="round"
            aria-hidden="true"
          >
            <path d="M15 3h6v6" />
            <path d="M10 14 21 3" />
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" />
          </svg>
        </Link>
        <button {...stylex.props(closeBtn.base)} onClick={onClose} title="Close">
          ×
        </button>
      </header>
      {/* Keyed so switching sub-agents starts from a clean stream and scroll. */}
      <Suspense
        key={subagent.conversationId}
        fallback={<p {...stylex.props(styles.notice)}>Loading conversation…</p>}
      >
        <SubagentQuery id={subagent.conversationId} status={subagent.status} />
      </Suspense>
    </aside>
  );
}

function SubagentQuery({id, status}: {id: string; status: string}) {
  const queryData = useLazyLoadQuery<TConversationPageQuery>(
    conversationPageQuery,
    {id: encodeGlobalId('Conversation', id), count: CONVERSATION_PAGE_SIZE, cursor: null},
    {fetchPolicy: 'store-and-network'},
  );
  if (!queryData.conversation) {
    return <p {...stylex.props(styles.notice)}>This sub-agent's conversation is gone.</p>;
  }
  return <SubagentConversation id={id} status={status} conversation={queryData.conversation} />;
}

/** Close enough to the bottom that new output should keep it in view. */
const FOLLOW_SLACK = 48;

function SubagentConversation({
  id,
  status,
  conversation,
}: {
  id: string;
  status: string;
  conversation: ConversationPageFragment$key;
}) {
  const toast = useToast();
  const {data, loadPrevious, hasPrevious, isLoadingPrevious} = usePaginationFragment<
    ConversationPageRefetchQuery,
    ConversationPageFragment$key
  >(conversationPageFragment, conversation);

  const allMessages = useMemo<Message[]>(
    () => data.messages.edges.map((e) => mapMessage(e.node)),
    [data.messages.edges],
  );
  const runningMsg = allMessages.find((m) => m.role === 'assistant' && m.status === 'running');

  // A turn started from this panel streams before its row has been refetched.
  const [pendingTaskId, setPendingTaskId] = useState<string | null>(null);
  useEffect(() => {
    if (pendingTaskId && allMessages.some((m) => m.id === pendingTaskId)) setPendingTaskId(null);
  }, [pendingTaskId, allMessages]);
  const streamTaskId = pendingTaskId ?? runningMsg?.id ?? null;

  const {streaming, text, thinkingText, steps, workers, error, pendingInterrupt} = useTaskEvents(
    streamTaskId,
    id,
  );

  // The parent's page is what hears that this sub-agent was given a task or
  // finished one (its `subagents` list). Nothing streams here until this
  // conversation has the new turn's row, so each change of status asks for it.
  useEffect(() => {
    void loadConversationPage(id);
  }, [id, status]);

  const isActive = streaming || !!runningMsg;
  const messages = allMessages.filter(
    (m) => !(m.role === 'assistant' && m.status === 'running') && m.status !== 'queued',
  );
  const queuedMessages = allMessages.filter((m) => m.status === 'queued');
  const canQueue = isActive && !!streamTaskId && !pendingInterrupt;

  // Follow the newest output while the reader is at the bottom.
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const followRef = useRef(true);
  useLayoutEffect(() => {
    const el = scrollerRef.current;
    if (el && followRef.current) el.scrollTop = el.scrollHeight;
  }, [allMessages, text, thinkingText, steps, isActive]);

  function loadOlder() {
    const el = scrollerRef.current;
    const prevHeight = el?.scrollHeight ?? 0;
    const prevTop = el?.scrollTop ?? 0;
    followRef.current = false;
    loadPrevious(CONVERSATION_PAGE_SIZE, {
      onComplete: () => {
        requestAnimationFrame(() => {
          if (el) el.scrollTop = prevTop + (el.scrollHeight - prevHeight);
        });
      },
    });
  }

  async function handleSubmit(
    query: string,
    model: string,
    effort: string | null,
    attachments: UploadedFile[],
  ) {
    followRef.current = true;
    try {
      const {taskId} = await commitStartTask({
        input: {
          query,
          model,
          effort,
          conversationId: id,
          attachmentUploads: attachments.map((a) => ({uploadId: a.uploadId})),
        },
      });
      setPendingTaskId(taskId);
      await loadConversationPage(id);
    } catch (err) {
      toast.push((err as Error).message || 'Could not send that message.', 'error');
    }
  }

  async function handleQueue(query: string) {
    if (!streamTaskId) return;
    try {
      await commitQueueMessage(streamTaskId, query);
      await loadConversationPage(id);
    } catch (err) {
      toast.push((err as Error).message || 'Could not queue that message.', 'error');
    }
  }

  async function handleUnqueue(messageId: string) {
    if (!streamTaskId) return;
    try {
      await commitUnqueueMessage(streamTaskId, messageId);
    } catch (err) {
      console.error('Failed to withdraw queued message:', err);
    }
    await loadConversationPage(id);
  }

  async function handleStop() {
    if (!streamTaskId) return;
    try {
      await commitStopTask(streamTaskId);
    } catch (err) {
      console.error('Failed to stop task:', err);
    }
  }

  return (
    <>
      <div
        ref={scrollerRef}
        {...stylex.props(styles.scroller)}
        onScroll={(e) => {
          const el = e.currentTarget;
          followRef.current = el.scrollHeight - el.clientHeight - el.scrollTop <= FOLLOW_SLACK;
        }}
      >
        {hasPrevious && (
          <button
            type="button"
            {...stylex.props(styles.older)}
            onClick={loadOlder}
            disabled={isLoadingPrevious}
          >
            {isLoadingPrevious ? 'Loading older messages…' : 'Show older messages'}
          </button>
        )}
        {messages.length === 0 && !isActive && (
          <p {...stylex.props(styles.notice)}>Nothing here yet.</p>
        )}
        {messages.map((msg) => (
          <MessageBubble key={msg.id} message={msg} />
        ))}
        {isActive && (
          <StreamingBubble
            text={text}
            ended={!streaming}
            thinkingText={thinkingText}
            steps={steps}
            workers={workers}
          />
        )}
        {queuedMessages.length > 0 && (
          <QueuedMessages
            messages={queuedMessages}
            onUnqueue={canQueue ? handleUnqueue : undefined}
          />
        )}
        {error && !isActive && (
          <div {...stylex.props(turn.base)}>
            <div {...stylex.props(errorBubble.base)}>⚠ {error}</div>
          </div>
        )}
      </div>
      <footer {...stylex.props(styles.footer)}>
        {pendingInterrupt && runningMsg && (
          <InterruptPrompt key={pendingInterrupt.approvalId} interrupt={pendingInterrupt} />
        )}
        <InputBox
          onSubmit={handleSubmit}
          disabled={isActive && !canQueue}
          queueing={canQueue}
          onQueue={handleQueue}
          onStop={handleStop}
          conversationId={id}
          initialModel={data.model}
          initialEffort={data.effort}
          fullWidth
        />
      </footer>
    </>
  );
}

const styles = stylex.create({
  root: {
    position: 'fixed',
    insetBlock: 0,
    insetInlineEnd: 0,
    // No `width` here on purpose: useResizableWidth owns it.
    maxWidth: {default: '90vw', '@media (max-width: 860px)': '100%'},
    paddingBlockStart: {default: null, '@media (max-width: 860px)': css.safeTop},
    backgroundColor: colors.bg,
    borderInlineStartWidth: {default: 1, '@media (max-width: 860px)': 0},
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.glassBorder,
    boxShadow: `-4px 0 28px rgba(${channels.shadow}, 0.3)`,
    display: 'flex',
    flexDirection: 'column',
    zIndex: 30,
    animationName: kf.slideIn,
    animationDuration: '180ms',
    animationFillMode: 'forwards',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: 12,
    paddingInline: '16px 12px',
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  title: {
    flex: 1,
    minWidth: 0,
    fontSize: type.tUi,
    fontWeight: 600,
    color: colors.text,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  status: {flex: 'none', fontSize: type.tSmall, color: colors.textDim, marginInlineEnd: 4},
  scroller: {
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    paddingBlock: 20,
    paddingInline: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 20,
    '::-webkit-scrollbar': {width: 6},
    '::-webkit-scrollbar-track': {backgroundColor: 'transparent'},
    '::-webkit-scrollbar-thumb': {
      backgroundColor: `rgba(${channels.tint}, 0.12)`,
      borderRadius: 2,
    },
  },
  older: {
    alignSelf: 'center',
    borderWidth: 0,
    backgroundColor: 'transparent',
    fontFamily: 'inherit',
    fontSize: type.tSmall,
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
    paddingBlock: 4,
  },
  notice: {
    margin: 0,
    padding: 24,
    textAlign: 'center',
    fontSize: type.tSmall,
    color: colors.textDim,
  },
  footer: {
    flexShrink: 0,
    paddingBlockStart: 8,
    paddingBlockEnd: `calc(12px + ${css.safeBottom})`,
    paddingInline: 12,
  },
});
