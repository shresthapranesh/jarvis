import {useEffect, useRef, useState} from 'react';
import {createPortal} from 'react-dom';

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

  const confirmable =
    !requireTypedName || typed.trim() === requireTypedName.trim();

  return createPortal(
    <div className="confirm-backdrop" onClick={onCancel}>
      <div
        className={`confirm-modal${danger ? ' confirm-modal--danger' : ''}`}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="confirm-title"
      >
        <div className="confirm-title" id="confirm-title">
          {title}
        </div>
        <div className="confirm-body">{message}</div>
        {requireTypedName && (
          <div className="confirm-typed">
            <label className="confirm-typed-label">
              Type <strong>{requireTypedName}</strong> to confirm
            </label>
            <input
              ref={inputRef}
              className="confirm-typed-input"
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
        <div className="confirm-actions">
          <button
            ref={cancelRef}
            className="confirm-btn confirm-btn--cancel"
            onClick={onCancel}
          >
            {cancelLabel}
          </button>
          <button
            className={`confirm-btn confirm-btn--confirm${danger ? ' confirm-btn--danger' : ''}`}
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
