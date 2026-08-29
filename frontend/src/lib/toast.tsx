import * as stylex from '@stylexjs/stylex';
import {createContext, useCallback, useContext, useMemo, useState, type ReactNode} from 'react';
import {createPortal} from 'react-dom';

import {AlertIcon, CheckIcon, InfoIcon, XIcon} from '../components/icons';
import {kf} from '../theme/keyframes.stylex';
import {channels, colors, layout, type} from '../theme/tokens.stylex';

export type ToastKind = 'success' | 'error' | 'info';

export interface Toast {
  id: number;
  kind: ToastKind;
  message: string;
}

interface ToastApi {
  push: (message: string, kind?: ToastKind) => void;
}

const ToastContext = createContext<ToastApi | null>(null);

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error('useToast must be used inside <ToastProvider>');
  return ctx;
}

let nextId = 1;

export function ToastProvider({children}: {children: ReactNode}) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((message: string, kind: ToastKind = 'info') => {
    const id = nextId++;
    setToasts((prev) => [...prev, {id, kind, message}]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 4000);
  }, []);

  const dismiss = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id));
  }, []);

  const api = useMemo(() => ({push}), [push]);

  return (
    <ToastContext.Provider value={api}>
      {children}
      {createPortal(
        <div {...stylex.props(styles.stack)} role="status" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} {...stylex.props(styles.item, styles[t.kind])}>
              <span {...stylex.props(styles.icon)}>
                {t.kind === 'success' && <CheckIcon size={14} />}
                {t.kind === 'error' && <AlertIcon size={14} />}
                {t.kind === 'info' && <InfoIcon size={14} />}
              </span>
              <span {...stylex.props(styles.message)}>{t.message}</span>
              <button
                {...stylex.props(styles.dismiss)}
                onClick={() => dismiss(t.id)}
                aria-label="Dismiss"
              >
                <XIcon size={12} />
              </button>
            </div>
          ))}
        </div>,
        document.body,
      )}
    </ToastContext.Provider>
  );
}

const styles = stylex.create({
  stack: {
    position: 'fixed',
    insetBlockStart: 16,
    insetInlineEnd: 16,
    zIndex: 300,
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    pointerEvents: 'none',
    maxWidth: 'calc(100vw - 32px)',
  },
  item: {
    display: 'flex',
    alignItems: 'center',
    gap: 9,
    backgroundColor: colors.glassBg,
    backdropFilter: layout.blur,
    WebkitBackdropFilter: layout.blur,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.glassBorder,
    borderInlineStartWidth: 3,
    borderRadius: 3,
    paddingBlock: 9,
    paddingInlineStart: 11,
    paddingInlineEnd: 12,
    minWidth: 240,
    maxWidth: 380,
    fontSize: type.tUi,
    color: colors.text,
    boxShadow: `0 6px 22px rgba(${channels.shadow}, 0.4)`,
    pointerEvents: 'auto',
    animationName: kf.toastIn,
    animationDuration: '0.34s',
    animationTimingFunction: 'cubic-bezier(0.32, 0.72, 0, 1)',
    animationFillMode: 'both',
  },
  // The kind tints both the spine and the glyph; the glyph reads it back off
  // this custom property, since it cannot select on its parent's variant.
  success: {borderInlineStartColor: colors.ok, '--toast-icon-color': colors.ok},
  error: {borderInlineStartColor: colors.danger, '--toast-icon-color': colors.danger},
  info: {borderInlineStartColor: colors.accent, '--toast-icon-color': colors.accent},

  icon: {display: 'inline-flex', flexShrink: 0, color: 'var(--toast-icon-color)'},
  message: {flex: 1, minWidth: 0},
  dismiss: {
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    borderStyle: 'none',
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
    padding: 3,
    borderRadius: 2,
    display: 'inline-flex',
    flexShrink: 0,
    transition: 'background 0.1s, color 0.1s',
  },
});
