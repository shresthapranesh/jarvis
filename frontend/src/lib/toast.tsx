import {createContext, useCallback, useContext, useMemo, useState, type ReactNode} from 'react';
import {createPortal} from 'react-dom';

import {AlertIcon, CheckIcon, InfoIcon, XIcon} from '../components/icons';

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
        <div className="toast-stack" role="status" aria-live="polite">
          {toasts.map((t) => (
            <div key={t.id} className={`toast-item toast-item--${t.kind}`}>
              <span className="toast-icon">
                {t.kind === 'success' && <CheckIcon size={14} />}
                {t.kind === 'error' && <AlertIcon size={14} />}
                {t.kind === 'info' && <InfoIcon size={14} />}
              </span>
              <span className="toast-message">{t.message}</span>
              <button
                className="toast-dismiss"
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
