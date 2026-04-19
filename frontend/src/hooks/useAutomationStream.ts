import {useQueryClient} from '@tanstack/react-query';
import {useEffect, useRef, useState} from 'react';

interface AutomationStreamState {
  streaming: boolean;
  text: string;
  error: string | null;
}

export function useAutomationStream(runId: string | null, automationId: string | null) {
  const queryClient = useQueryClient();
  const abortRef = useRef<AbortController | null>(null);
  const [state, setState] = useState<AutomationStreamState>({
    streaming: false,
    text: '',
    error: null,
  });

  useEffect(() => {
    if (!runId) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setState({streaming: true, text: '', error: null});

    async function consume() {
      try {
        const res = await fetch(`/stream/automation/${runId}`, {signal: controller.signal});
        if (!res.ok) throw new Error(`Stream error ${res.status}`);

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

            let parsed: Record<string, unknown>;
            try {
              parsed = JSON.parse(data);
            } catch {
              continue;
            }

            if (eventType === 'token') {
              setState((s) => ({...s, text: s.text + (parsed['text'] as string)}));
            } else if (eventType === 'done') {
              const output = parsed['output'] as string | undefined;
              if (output !== undefined) {
                setState((s) => ({...s, text: output, streaming: false}));
              } else {
                setState((s) => ({...s, streaming: false}));
              }
              if (automationId) {
                await queryClient.invalidateQueries({
                  queryKey: ['automation-runs', automationId],
                });
              }
            } else if (eventType === 'error') {
              setState((s) => ({...s, streaming: false, error: parsed['error'] as string}));
            }
          }
        }
      } catch (err) {
        if ((err as Error).name !== 'AbortError') {
          setState((s) => ({...s, streaming: false, error: (err as Error).message}));
        } else {
          setState((s) => ({...s, streaming: false}));
        }
      }
    }

    void consume();

    return () => {
      abortRef.current?.abort();
    };
  }, [runId]); // eslint-disable-line react-hooks/exhaustive-deps

  return state;
}
