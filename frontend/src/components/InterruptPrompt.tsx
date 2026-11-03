import * as stylex from '@stylexjs/stylex';
import {useEffect, useRef, useState} from 'react';

import type {PendingInterrupt} from '../hooks/useTaskEvents';
import {commitResolveApproval} from '../relay/ResolveApprovalMutation';
import {channels, colors, type} from '../theme/tokens.stylex';

interface Props {
  /**
   * What the run is blocked on: a per-tool approval gate (edge/src/approvals.rs)
   * or the agent's `ask_user` question. The run is parked inside the tool call,
   * so the answer goes to the durable row.
   */
  interrupt: PendingInterrupt;
}

export function InterruptPrompt({interrupt}: Props) {
  const {kind, question, approvalId, options, multiSelect} = interrupt;
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [picked, setPicked] = useState<string[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  async function submit(answer: string) {
    answer = answer.trim();
    if (!answer || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      await commitResolveApproval(approvalId, answer);
      // The resolved event clears this prompt — no local state needed.
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  }

  // Several picks go as one answer, with anything typed after them.
  function submitAll() {
    const typed = textareaRef.current?.value.trim() ?? '';
    void submit([...picked, typed].filter(Boolean).join(', '));
  }

  function choose(label: string) {
    if (submitting) return;
    if (!multiSelect) {
      void submit(label);
      return;
    }
    setPicked((p) => (p.includes(label) ? p.filter((l) => l !== label) : [...p, label]));
  }

  // 1–9 pick an option while focus isn't in a field (the composer is disabled
  // while the run waits).
  const chooseRef = useRef(choose);
  chooseRef.current = choose;
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      const t = e.target as HTMLElement | null;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      if (t && (t.isContentEditable || ['INPUT', 'TEXTAREA', 'SELECT'].includes(t.tagName))) return;
      const n = Number(e.key);
      if (Number.isInteger(n) && n >= 1 && n <= options.length) {
        e.preventDefault();
        chooseRef.current(options[n - 1].label);
      }
    }
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [options]);

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      if (multiSelect) submitAll();
      else void submit(e.currentTarget.value);
    }
  }

  return (
    <div {...stylex.props(styles.wrap)}>
      <div {...stylex.props(styles.card)} role="group" aria-label={question}>
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
          <span>{kind === 'gate' ? 'Approval needed' : 'Agent has a question'}</span>
        </div>
        <div {...stylex.props(styles.question)}>{question}</div>
        {options.length > 0 && (
          <div {...stylex.props(styles.options)} role={multiSelect ? 'group' : 'list'}>
            {options.map((o, i) => {
              const on = picked.includes(o.label);
              return (
                <button
                  key={o.label}
                  type="button"
                  {...stylex.props(styles.option, on && styles.optionOn)}
                  disabled={submitting}
                  aria-pressed={multiSelect ? on : undefined}
                  onClick={() => choose(o.label)}
                >
                  <span {...stylex.props(styles.badge, on && styles.badgeOn)}>
                    {multiSelect && on ? '✓' : i + 1}
                  </span>
                  <span {...stylex.props(styles.optionText)}>
                    <span {...stylex.props(styles.optionLabel)}>{o.label}</span>
                    {o.description && (
                      <span {...stylex.props(styles.optionDesc)}>{o.description}</span>
                    )}
                  </span>
                </button>
              );
            })}
          </div>
        )}
        <textarea
          ref={textareaRef}
          {...stylex.props(styles.textarea)}
          rows={1}
          placeholder={options.length > 0 ? 'Or type your own answer…' : 'Type your answer…'}
          aria-label="Your answer"
          disabled={submitting}
          onKeyDown={handleKeyDown}
        />
        {error && <div {...stylex.props(styles.error)}>{error}</div>}
        <div {...stylex.props(styles.footer)}>
          <span {...stylex.props(styles.hint)}>
            {options.length > 0 ? `1–${options.length} to pick · ` : ''}
            Enter to send
          </span>
          <button
            type="button"
            {...stylex.props(styles.submit)}
            disabled={submitting}
            onClick={() =>
              multiSelect ? submitAll() : void submit(textareaRef.current?.value ?? '')
            }
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
  options: {display: 'flex', flexDirection: 'column', gap: 6},
  option: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: 10,
    width: '100%',
    textAlign: 'left',
    backgroundColor: {default: colors.surface, ':hover:not(:disabled)': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover:not(:disabled)': colors.borderStrong},
    borderRadius: 3,
    paddingBlock: 8,
    paddingInline: 10,
    color: colors.text,
    fontFamily: 'inherit',
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.55},
    transition: 'background-color 0.15s, border-color 0.15s',
  },
  optionOn: {
    borderColor: {default: colors.accent, ':hover:not(:disabled)': colors.accent},
    backgroundColor: {default: colors.accentDim, ':hover:not(:disabled)': colors.accentDim},
  },
  badge: {
    flexShrink: 0,
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 20,
    height: 20,
    marginTop: 1,
    borderRadius: 3,
    backgroundColor: colors.surface3,
    color: colors.textDim,
    fontSize: type.tSmall,
    fontFamily: type.mono,
  },
  badgeOn: {backgroundColor: colors.accent, color: colors.accentContrast},
  optionText: {display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0},
  optionLabel: {fontSize: type.tUi, fontWeight: 600, lineHeight: 1.5},
  optionDesc: {fontSize: type.tSmall, color: colors.textDim, lineHeight: 1.4},
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
    transition: 'box-shadow 0.15s, transform 0.15s, opacity 0.15s',
  },
});
