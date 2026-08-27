import * as stylex from '@stylexjs/stylex';
import {useEffect, useRef, useState} from 'react';
import {createPortal} from 'react-dom';

import {channels, colors, type} from '../theme/tokens.stylex';
import {modal} from './ui';

interface Props {
  open: boolean;
  title: string;
  message: React.ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  requireTypedName?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  danger = false,
  requireTypedName = null,
  onConfirm,
  onCancel,
}: Props) {
  const [typed, setTyped] = useState('');
  const cancelRef = useRef<HTMLButtonElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (open) {
      setTyped('');
      const t = setTimeout(() => {
        if (requireTypedName) inputRef.current?.focus();
        else cancelRef.current?.focus();
      }, 30);
      return () => clearTimeout(t);
    }
  }, [open, requireTypedName]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault();
        onCancel();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onCancel]);

  if (!open) return null;

  const confirmable = !requireTypedName || typed.trim() === requireTypedName.trim();

  return createPortal(
    <div {...stylex.props(modal.backdrop, styles.backdrop)} onClick={onCancel}>
      <div
        {...stylex.props(modal.panel, styles.panel, danger && styles.panelDanger)}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
      >
        <div {...stylex.props(styles.title)} id="confirm-title">
          {title}
        </div>
        {/* `message` is JSX handed in by the caller, so its <p>/<strong>
            descendants are styled from base.css — see the note there. */}
        <div {...stylex.props(styles.body)} data-dialog-prose>
          {message}
        </div>
        {requireTypedName && (
          <div {...stylex.props(styles.typed)}>
            <label {...stylex.props(styles.typedLabel)}>
              Type <strong {...stylex.props(styles.typedName)}>{requireTypedName}</strong> to
              confirm
            </label>
            <input
              ref={inputRef}
              {...stylex.props(styles.typedInput)}
              type="text"
              value={typed}
              onChange={(e) => setTyped(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && confirmable) {
                  e.preventDefault();
                  onConfirm();
                }
              }}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
        )}
        <div {...stylex.props(styles.actions)}>
          <button
            ref={cancelRef}
            {...stylex.props(styles.btn, styles.btnCancel)}
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            {...stylex.props(styles.btn, danger ? styles.btnDanger : styles.btnConfirm)}
            onClick={onConfirm}
            disabled={!confirmable}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

const styles = stylex.create({
  // A blur is expensive on a phone GPU and the sheet reads fine without it, so
  // touch layouts trade it for a heavier tint.
  backdrop: {
    backdropFilter: {default: 'blur(4px)', '@media (hover: none)': 'none'},
    WebkitBackdropFilter: {default: 'blur(4px)', '@media (hover: none)': 'none'},
    backgroundColor: {
      default: `rgba(${channels.shadow}, 0.55)`,
      '@media (hover: none)': `rgba(${channels.shadow}, 0.72)`,
    },
  },
  panel: {maxWidth: 440},
  panelDanger: {borderColor: `rgba(${channels.danger}, 0.3)`},

  title: {fontSize: '0.98rem', fontWeight: 600, color: colors.text, marginBlockEnd: 8},
  body: {fontSize: '0.85rem', color: colors.textDim, lineHeight: 1.5},

  typed: {marginBlockStart: 14, display: 'flex', flexDirection: 'column', gap: 6},
  typedLabel: {fontSize: '0.72rem', color: colors.textDim},
  typedName: {
    color: colors.text,
    fontFamily: type.mono,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    paddingInline: 5,
    borderRadius: 3,
    fontSize: '0.76rem',
  },
  typedInput: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 6,
    paddingBlock: 8,
    paddingInline: 10,
    color: colors.text,
    fontSize: '0.84rem',
    fontFamily: 'inherit',
    outline: 'none',
    transition: 'border-color 0.15s',
  },

  actions: {display: 'flex', justifyContent: 'flex-end', gap: 8, marginBlockStart: 18},
  btn: {
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    color: colors.text,
    fontFamily: 'inherit',
    fontSize: '0.8rem',
    paddingBlock: 8,
    paddingInline: 16,
    borderRadius: 7,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.4},
    transition: 'background 0.12s, border-color 0.12s',
  },
  btnCancel: {color: colors.textDim},
  btnConfirm: {
    backgroundColor: {default: colors.accent, ':hover:not(:disabled)': colors.accentStrong},
    borderColor: {default: colors.accent, ':hover:not(:disabled)': colors.accentStrong},
    color: colors.accentContrast,
    fontWeight: 500,
  },
  btnDanger: {
    backgroundColor: colors.danger,
    borderColor: colors.danger,
    color: '#fff',
    fontWeight: 500,
  },
});
