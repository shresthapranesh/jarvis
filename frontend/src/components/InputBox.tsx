import * as stylex from '@stylexjs/stylex';
import {useEffect, useRef, useState} from 'react';

import {useIsMobile} from '../hooks/useIsMobile';
import {useModels} from '../hooks/useModels';
import {refreshConversationList} from '../relay/ConversationListQuery';
import {commitUpdateConversation} from '../relay/UpdateConversationMutation';
import {composer, control} from './InputBox.styles';
import {field, iconBtn} from './ui';

interface Props {
  onSubmit: (query: string, model: string) => void;
  disabled?: boolean;
  /**
   * A run is in flight and sending queues instead of starting a second turn.
   * The composer stays live — this is the whole point of the mode — so
   * `disabled` is false here and every rule that keys off it stays off.
   */
  queueing?: boolean;
  /** Called instead of `onSubmit` while `queueing`. */
  onQueue?: (query: string) => void;
  onStop?: () => void;
  conversationId?: string;
  initialModel?: string;
  // Incognito toggle — only wired on the new-chat surface (index page). When
  // provided, an eye-off button lets the user start the conversation ephemeral.
  incognito?: boolean;
  onToggleIncognito?: () => void;
  /**
   * Drop the 760px measure. The dispatch screen already constrains the column
   * around it, so the composer should fill that column rather than sit narrower
   * inside it.
   */
  fullWidth?: boolean;
}

export function InputBox({
  onSubmit,
  disabled = false,
  queueing = false,
  onQueue,
  onStop,
  conversationId,
  initialModel,
  incognito = false,
  onToggleIncognito,
  fullWidth = false,
}: Props) {
  const {data: catalog} = useModels();
  const isMobile = useIsMobile();
  const [model, setModel] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  // Seed the model: prefer the per-conversation `initialModel`, then fall back
  // to the catalog default. Re-runs when the user navigates between
  // conversations so the dropdown reflects the conversation we're in.
  useEffect(() => {
    if (initialModel) {
      setModel(initialModel);
    } else if (catalog && !model) {
      setModel(catalog.default);
    }
  }, [catalog, initialModel]); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleModelChange(newModel: string) {
    setModel(newModel);
    if (conversationId && newModel) {
      try {
        await commitUpdateConversation(conversationId, {model: newModel});
        await refreshConversationList();
      } catch (err) {
        console.error('Failed to persist model change:', err);
      }
    }
  }

  function handleInput() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function clearInput() {
    const el = textareaRef.current!;
    el.value = '';
    el.style.height = 'auto';
  }

  function send() {
    const query = textareaRef.current?.value.trim();
    if (queueing) {
      if (!query || !onQueue) return;
      onQueue(query);
      clearInput();
      return;
    }
    if (!query || disabled || !model) return;
    onSubmit(query, model);
    clearInput();
  }

  return (
    <div {...stylex.props(composer.wrap, fullWidth && composer.wrapFull)}>
      <div
        {...stylex.props(
          composer.card,
          disabled && composer.cardDisabled,
          queueing && composer.cardQueueing,
          incognito && composer.cardIncognito,
        )}
      >
        <textarea
          ref={(el) => {
            textareaRef.current = el;
            el?.focus();
          }}
          {...stylex.props(composer.textarea)}
          data-input-textarea=""
          rows={1}
          placeholder={
            queueing
              ? 'Queue a message for this run…'
              : incognito
                ? 'Ask anything… (incognito — not saved)'
                : 'Ask anything…'
          }
          disabled={disabled}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
        />

        <div {...stylex.props(composer.footer)}>
          {onToggleIncognito && (
            <button
              type="button"
              {...stylex.props(iconBtn.base, control.glyph, incognito && control.iconActive)}
              title={
                incognito
                  ? 'Incognito on — this chat won’t be saved'
                  : 'Start an incognito chat (not saved)'
              }
              aria-pressed={incognito}
              disabled={disabled || queueing}
              onClick={onToggleIncognito}
            >
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
            </button>
          )}

          <select
            {...stylex.props(control.model, field.selectChrome)}
            value={model}
            onChange={(e) => void handleModelChange(e.target.value)}
            disabled={!catalog}
            title={catalog ? undefined : 'Loading models…'}
          >
            {!catalog && <option value="">Loading…</option>}
            {catalog?.available.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>

          {/* On touch there is no Enter/Shift+Enter to describe, and at the 16px
              control size the hint pushes the send button off screen. */}
          <span {...stylex.props(composer.hint, composer.hintIdle)}>
            {queueing
              ? 'Enter · delivered at the run’s next step'
              : isMobile
                ? ''
                : 'Enter · Shift+Enter for newline'}
          </span>

          {onStop && (disabled || queueing) ? (
            <>
              <button
                {...stylex.props(control.send, control.sendStop)}
                onClick={onStop}
                title="Stop"
              >
                <svg
                  width="14"
                  height="14"
                  viewBox="0 0 24 24"
                  fill="currentColor"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <rect x="6" y="6" width="12" height="12" rx="2" ry="2" />
                </svg>
              </button>
              {queueing && (
                <button {...stylex.props(control.send)} onClick={send} title="Queue message">
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
                    <line x1="22" y1="2" x2="11" y2="13" />
                    <polygon points="22 2 15 22 11 13 2 9 22 2" />
                  </svg>
                </button>
              )}
            </>
          ) : (
            <button {...stylex.props(control.send)} onClick={send} disabled={disabled} title="Send">
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
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
