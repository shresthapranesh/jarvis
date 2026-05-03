import type {SafetyBlock} from '../hooks/useStream';
import type {Message, Step} from '../lib/types';
import {MessageBubble, StreamingBubble} from './MessageBubble';

interface Props {
  messages: Message[];
  streamingText?: string;
  streamingThinkingText?: string;
  streamingSteps?: Step[];
  isStreaming?: boolean;
  streamError?: string;
  streamSafetyBlock?: SafetyBlock | null;
  bottomRef: React.RefObject<HTMLDivElement | null>;
  onShowSteps?: (steps: Step[]) => void;
}

export function MessageThread({
  messages,
  streamingText,
  streamingThinkingText,
  streamingSteps,
  isStreaming,
  streamError,
  streamSafetyBlock,
  bottomRef,
  onShowSteps,
}: Props) {
  return (
    <div id="messages">
      {messages.map((msg) => (
        <MessageBubble key={msg.id} message={msg} onShowSteps={onShowSteps} />
      ))}
      {isStreaming && (
        <StreamingBubble
          text={streamingText ?? ''}
          thinkingText={streamingThinkingText ?? ''}
          steps={streamingSteps ?? []}
          safetyBlock={streamSafetyBlock ?? null}
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
