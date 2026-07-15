import type {WorkerInfo} from '../hooks/useTaskEvents';
import type {Message, Step, TodoItem} from '../lib/types';
import {MessageBubble, StreamingBubble} from './MessageBubble';
import {TodoList} from './TodoList';

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
}: Props) {
  return (
    <div id="messages" ref={containerRef}>
      {topRef && <div ref={topRef} className="messages-top-sentinel" />}
      {isLoadingOlder && (
        <div className="messages-loading-older">Loading older messages…</div>
      )}
      {messages.map((msg) => (
        <MessageBubble key={msg.id} message={msg} onShowSteps={onShowSteps} />
      ))}
      {todos && todos.length > 0 && (
        <div className="turn">
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
        />
      )}
      {streamError && !isStreaming && (
        <div className="turn">
          <div className="error-bubble">⚠ {streamError}</div>
        </div>
      )}
      <div ref={bottomRef} />
    </div>
  );
}
