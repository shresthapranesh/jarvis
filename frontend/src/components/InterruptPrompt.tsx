import * as stylex from '@stylexjs/stylex';
import {useRef, useState} from 'react';

import {commitResolveApproval} from '../relay/ResolveApprovalMutation';
import {commitResumeTask} from '../relay/ResumeTaskMutation';
import {channels, colors, type} from '../theme/tokens.stylex';

interface Props {
  taskId: string;
  question: string;
  /**
   * Set when the pause is a per-tool approval gate (core/tool_gate.py). The run
   * is blocked inside the tool call, not on a LangGraph interrupt, so the
   * answer goes to the durable row — `resumeTask` would find no interrupt to
   * resume and the call would stay parked until it times out.
   */
  approvalId?: string;
}

export function InterruptPrompt({taskId, question, approvalId}: Props) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  async function submit() {
    const answer = textareaRef.current?.value.trim() ?? '';
    if (!answer || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      if (approvalId) {
        await commitResolveApproval(approvalId, answer);
      } else {
        await commitResumeTask(taskId, answer);
      }
      // The approval_resolved / interrupt_resolved event clears this prompt —
      // no local state needed.
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div {...stylex.props(styles.wrap)}>
      <div {...stylex.props(styles.card)}>
        <div {...stylex.props(styles.header)}>
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span>Agent needs your input</span>
        </div>
        <div {...stylex.props(styles.question)}>{question}</div>
        <textarea
          ref={(el) => {
            textareaRef.current = el;
            el?.focus();
          }}
          {...stylex.props(styles.textarea)}
          rows={2}
          placeholder="Type your answer…"
          disabled={submitting}
          onKeyDown={handleKeyDown}
        />
        {error && <div {...stylex.props(styles.error)}>{error}</div>}
        <div {...stylex.props(styles.footer)}>
          <span {...stylex.props(styles.hint)}>Enter to submit · Shift+Enter for newline</span>
          <button
            type="button"
            {...stylex.props(styles.submit)}
            disabled={submitting}
            onClick={submit}
          >
            {submitting ? 'Sending…' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  );
}

const styles = stylex.create({
  wrap: {maxWidth: 760, marginBlock: '0 10px', marginInline: 'auto'},
  card: {
    backgroundColor: colors.glassBg,
    backdropFilter: 'blur(16px)',
    WebkitBackdropFilter: 'blur(16px)',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: `rgba(${channels.accent}, 0.35)`,
    borderRadius: 3,
    display: 'flex',
    flexDirection: 'column',
    paddingBlock: 12,
    paddingInline: 14,
    gap: 8,
    boxShadow: `0 4px 24px rgba(${channels.shadow}, 0.3)`,
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    fontSize: type.tSmall,
    color: colors.accent,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
  },
  question: {color: colors.text, fontSize: type.tBody, lineHeight: 1.45, whiteSpace: 'pre-wrap'},
  textarea: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 3,
    outline: 'none',
    resize: 'none',
    color: colors.text,
    fontSize: type.tBody,
    fontFamily: 'inherit',
    lineHeight: 1.5,
    paddingBlock: 8,
    paddingInline: 10,
    minHeight: 40,
    maxHeight: 120,
    '::placeholder': {color: colors.textDim},
  },
  error: {color: colors.errorText, fontSize: type.tUi},
  footer: {display: 'flex', alignItems: 'center', gap: 8},
  hint: {flex: 1, fontSize: type.tMicro, color: colors.textDim},
  submit: {
    backgroundImage: `linear-gradient(135deg, ${colors.accentStrong}, ${colors.accent})`,
    color: colors.accentContrast,
    borderStyle: 'none',
    borderRadius: 3,
    paddingBlock: 6,
    paddingInline: 14,
    fontSize: type.tUi,
    fontWeight: 600,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.45},
    boxShadow: {
      default: `0 2px 10px rgba(${channels.accent}, 0.3)`,
      ':hover:not(:disabled)': `0 4px 16px rgba(${channels.accent}, 0.45)`,
    },
    transform: {
      default: null,
      ':hover:not(:disabled)': 'translateY(-1px)',
      ':active:not(:disabled)': 'scale(0.97)',
    },
    transition: 'box-shadow 0.2s, transform 0.15s, opacity 0.15s',
  },
});
