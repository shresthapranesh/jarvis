import {useEffect} from 'react';
import {createPortal} from 'react-dom';

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
    <div className="confirm-backdrop" onClick={onClose}>
      <div
        className={`form-modal${wide ? ' form-modal--wide' : ''}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="form-modal-title"
      >
        <header className="form-modal-head">
          <h2 className="form-modal-title" id="form-modal-title">
            {title}
          </h2>
          {subtitle && <p className="form-modal-subtitle">{subtitle}</p>}
        </header>
        <div className="form-modal-fields">{children}</div>
        {error && <div className="memory-error">{error}</div>}
        <footer className="form-modal-footer">
          <div className="form-modal-footer-extra">{footerExtra}</div>
          <div className="form-modal-footer-actions">
            <span className="form-modal-kbd-hint" aria-hidden="true">
              <kbd>⌘↩</kbd> to save
            </span>
            <button className="artifact-btn" onClick={onClose}>
              Cancel
            </button>
            <button
              className="artifact-btn primary"
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
