import * as stylex from '@stylexjs/stylex';

import type {WorkerInfo} from '../hooks/useTaskEvents';
import type {ArtifactCard, Message, Step, TodoItem} from '../lib/types';
import {channels, layout, space, type} from '../theme/tokens.stylex';
import {MessageBubble, StreamingBubble} from './MessageBubble';
import {QueuedMessages} from './QueuedMessages';
import {TodoList} from './TodoList';
import {errorBubble, turn} from './ui';

interface Props {
  messages: Message[];
  streamingText?: string;
  streamingThinkingText?: string;
  streamingSteps?: Step[];
  streamingWorkers?: WorkerInfo[];
  isStreaming?: boolean;
  streamError?: string;
  todos?: TodoItem[];
  bottomRef: React.RefObject<HTMLDivElement | null>;
  topRef?: React.RefObject<HTMLDivElement | null>;
  containerRef?: React.RefObject<HTMLDivElement | null>;
  isLoadingOlder?: boolean;
  onShowSteps?: (steps: Step[]) => void;
  /**
   * Whether the run spine is showing beside the thread. The spine is absolutely
   * positioned, so the thread reserves its width as padding — the old rule was
   * `.page.has-spine #messages`, which no compiled style can express.
   */
  hasSpine?: boolean;
  spineCollapsed?: boolean;
  /** Artifacts keyed by the assistant message that produced them. */
  artifactsByMessage?: Map<string, ArtifactCard[]>;
  /** Artifacts the in-flight run has produced so far. */
  streamingArtifacts?: ArtifactCard[];
  onOpenArtifact?: (id: string) => void;
  openArtifactId?: string | null;
  /** The browser this turn touched, if any — drives the chip. */
  browsing?: {host: string; live: boolean} | null;
  onOpenBrowser?: () => void;
  /** Messages typed during the run and not yet delivered to it. */
  queuedMessages?: Message[];
  onUnqueue?: (messageId: string) => void;
}

export function MessageThread({
  messages,
  streamingText,
  streamingThinkingText,
  streamingSteps,
  streamingWorkers,
  isStreaming,
  streamError,
  todos,
  bottomRef,
  topRef,
  containerRef,
  isLoadingOlder,
  onShowSteps,
  hasSpine,
  spineCollapsed,
  artifactsByMessage,
  streamingArtifacts,
  browsing,
  onOpenBrowser,
  onOpenArtifact,
  openArtifactId,
  queuedMessages,
  onUnqueue,
}: Props) {
  return (
    <div
      id="messages"
      ref={containerRef}
      {...stylex.props(
        styles.scroller,
        hasSpine && styles.scrollerWithSpine,
        hasSpine && spineCollapsed && styles.scrollerWithSpineCollapsed,
      )}
    >
      {topRef && <div ref={topRef} {...stylex.props(styles.topSentinel)} />}
      {isLoadingOlder && <div {...stylex.props(styles.loadingOlder)}>Loading older messages…</div>}
      {messages.map((msg) => (
        <MessageBubble
          key={msg.id}
          message={msg}
          onShowSteps={onShowSteps}
          artifacts={artifactsByMessage?.get(msg.id)}
          onOpenArtifact={onOpenArtifact}
          openArtifactId={openArtifactId}
        />
      ))}
      {todos && todos.length > 0 && (
        <div {...stylex.props(turn.base)}>
          <TodoList todos={todos} />
        </div>
      )}
      {isStreaming && (
        <StreamingBubble
          text={streamingText ?? ''}
          thinkingText={streamingThinkingText ?? ''}
          steps={streamingSteps ?? []}
          workers={streamingWorkers ?? []}
          onShowSteps={onShowSteps}
          browsing={browsing}
          onOpenBrowser={onOpenBrowser}
          artifacts={streamingArtifacts}
          onOpenArtifact={onOpenArtifact}
          openArtifactId={openArtifactId}
        />
      )}
      {queuedMessages && queuedMessages.length > 0 && (
        <QueuedMessages messages={queuedMessages} onUnqueue={onUnqueue} />
      )}
      {streamError && !isStreaming && (
        <div {...stylex.props(turn.base)}>
          <div {...stylex.props(errorBubble.base)}>⚠ {streamError}</div>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}

const styles = stylex.create({
  scroller: {
    flex: 1,
    overflowY: 'auto',
    paddingBlock: {default: 28, '@media (max-width: 768px)': 20},
    paddingInline: {default: 20, '@media (max-width: 768px)': 14},
    display: 'flex',
    flexDirection: 'column',
    gap: 24,
    /* No `scroll-behavior: smooth` here on purpose. It applies to every
       programmatic scroll that doesn't name a behavior — including the
       restore-to-bottom on open and the scrollTop fixup after prepending older
       messages, both of which must be instant. Callers that want the animation
       pass `behavior: 'smooth'` explicitly (see routes/c.$id.tsx). */
    '::-webkit-scrollbar': {width: 6},
    '::-webkit-scrollbar-track': {backgroundColor: 'transparent'},
    '::-webkit-scrollbar-thumb': {
      backgroundColor: `rgba(${channels.tint}, 0.12)`,
      borderRadius: 2,
    },
  },
  scrollerWithSpine: {
    paddingInlineEnd: {
      default: `calc(${layout.spineW} + ${space.s5})`,
      // Below this the rail is hidden entirely, so the reservation goes too.
      '@media (max-width: 1100px)': space.s5,
    },
  },
  /**
   * A settled rail is 30px, so reserving 208 for it would give away the width
   * collapsing it was meant to reclaim. Applied after `scrollerWithSpine` so
   * it wins. Hovering the rail expands it *over* this padding rather than
   * pushing it — the rail is `position: fixed`, so nothing reflows.
   */
  scrollerWithSpineCollapsed: {
    paddingInlineEnd: {
      default: `calc(${layout.spineCollapsedW} + ${space.s5})`,
      '@media (max-width: 1100px)': space.s5,
    },
  },
  topSentinel: {height: 1, flexShrink: 0},
  loadingOlder: {
    alignSelf: 'center',
    fontSize: type.tSmall,
    color: `rgba(${channels.tint}, 0.45)`,
    paddingBlock: 4,
  },
});
