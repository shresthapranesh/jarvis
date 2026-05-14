import {useEffect, useRef, useState} from 'react';

import type {LogRecord} from '../lib/types';

const CAP = 2000;

interface LogStreamState {
  logs: LogRecord[];
  connected: boolean;
  error: string | null;
}

const EMPTY: LogStreamState = {logs: [], connected: false, error: null};

export function useLogStream(enabled: boolean) {
  const [state, setState] = useState<LogStreamState>(EMPTY);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    if (!enabled) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    async function consume() {
      try {
        const res = await fetch('/server-logs/stream', {signal: controller.signal});
        if (!res.ok) throw new Error(`Stream error ${res.status}`);

        setState((s) => ({...s, connected: true, error: null}));

        const reader = res.body!.getReader();
        const decoder = new TextDecoder();
        let buf = '';

        while (true) {
          const {done, value} = await reader.read();
          if (done) break;

          buf += decoder.decode(value, {stream: true});
          const parts = buf.split(/\r?\n\r?\n/);
          buf = parts.pop() ?? '';

          for (const part of parts) {
            if (!part.trim()) continue;

            let eventType = 'message';
            let data = '';
            for (const line of part.split('\n')) {
              if (line.startsWith('event: ')) eventType = line.slice(7).trim();
              else if (line.startsWith('data: ')) data = line.slice(6).trim();
            }
            if (!data) continue;

            if (eventType === 'backfill') {
              try {
                const records = JSON.parse(data) as LogRecord[];
                setState((s) => ({...s, logs: records.slice(-CAP)}));
              } catch {
                // ignore malformed backfill
              }
            } else if (eventType === 'log') {
              try {
                const record = JSON.parse(data) as LogRecord;
                setState((s) => {
                  const next = s.logs.length >= CAP ? s.logs.slice(s.logs.length - CAP + 1) : s.logs;
                  return {...s, logs: [...next, record]};
                });
              } catch {
                // ignore malformed record
              }
            }
            // 'ping' is a keepalive; ignore.
          }
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          setState((s) => ({...s, connected: false, error: (err as Error).message}));
        } else {
          setState((s) => ({...s, connected: false}));
        }
      }
    }

    void consume();

    return () => {
      abortRef.current?.abort();
    };
  }, [enabled]);

  return state;
}
