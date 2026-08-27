import * as stylex from '@stylexjs/stylex';
import {useEffect} from 'react';
import {createPortal} from 'react-dom';

import {colors} from '../theme/tokens.stylex';
import {btn, modal, page} from './ui';

interface Props {
  open: boolean;
  title: string;
  subtitle?: React.ReactNode;
  wide?: boolean;
  submitLabel: string;
  submitDisabled?: boolean;
  pending?: boolean;
  error?: string | null;
  footerExtra?: React.ReactNode;
  onSubmit: () => void;
  onClose: () => void;
  children: React.ReactNode;
}

/** Shared add/edit dialog shell — Esc closes, ⌘/Ctrl+Enter submits. */
export function FormModal({
  open,
  title,
  subtitle,
  wide = false,
  submitLabel,
  submitDisabled = false,
  pending = false,
  error,
  footerExtra,
  onSubmit,
  onClose,
  children,
}: Props) {
  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        e.preventDefault();
        onClose();
      } else if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        if (!submitDisabled && !pending) onSubmit();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose, onSubmit, submitDisabled, pending]);

  if (!open) return null;

  return createPortal(
    <div {...stylex.props(modal.backdrop)} onClick={onClose}>
      <div
        {...stylex.props(modal.panel, formModal.panel, wide && formModal.panelWide)}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="form-modal-title"
      >
        <header>
          <h2 {...stylex.props(modal.title)} id="form-modal-title">
            {title}
          </h2>
          {subtitle && <p {...stylex.props(modal.subtitle)}>{subtitle}</p>}
        </header>
        <div {...stylex.props(formModal.fields)}>{children}</div>
        {error && <div {...stylex.props(page.error)}>{error}</div>}
        <footer {...stylex.props(formModal.footer)}>
          <div>{footerExtra}</div>
          <div {...stylex.props(formModal.footerActions)}>
            <span {...stylex.props(formModal.kbdHint)} aria-hidden="true">
              <kbd {...stylex.props(modal.kbd)}>⌘↩</kbd> to save
            </span>
            <button {...stylex.props(btn.base)} onClick={onClose}>
              Cancel
            </button>
            <button
              {...stylex.props(btn.base, btn.primary)}
              onClick={onSubmit}
              disabled={submitDisabled || pending}
            >
              {pending ? 'Saving…' : submitLabel}
            </button>
          </div>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

/**
 * The shell's own styles, exported because ModelSyncModal hand-rolls the
 * same dialog — its body is a scrolling report rather than a field stack,
 * so it cannot use this component, but it should not look different.
 */
export const formModal = stylex.create({
  panel: {
    borderColor: colors.borderStrong,
    maxWidth: 520,
    // 100vh does not account for a phone's collapsing URL bar; 100dvh does.
    maxHeight: {default: 'calc(100vh - 64px)', '@media (hover: none)': 'calc(100dvh - 64px)'},
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: 16,
  },
  panelWide: {maxWidth: 680},
  fields: {display: 'flex', flexDirection: 'column', gap: 14},
  footer: {display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 12},
  footerActions: {display: 'flex', alignItems: 'center', gap: 8, marginInlineStart: 'auto'},
  kbdHint: {fontSize: '0.7rem', color: colors.textFaint, marginInlineEnd: 4},
});
