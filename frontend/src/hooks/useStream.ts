import {useQueryClient} from '@tanstack/react-query';
import {useEffect, useRef, useState} from 'react';

import type {Step} from '../lib/types';

export interface BrowserStep {
  thought: unknown;
  actions: unknown[];
  at: string;
}

interface StreamState {
  streaming: boolean;
  text: string;
  thinkingText: string;
  steps: Step[];
  browserSteps: BrowserStep[];
  error: string | null;
  pendingInterrupt: {id: string; question: string} | null;
}

export function useStream(taskId: string | null, conversationId: string | null) {
  const queryClient = useQueryClient();
  const abortRef = useRef<AbortController | null>(null);
  const [state, setState] = useState<StreamState>({
    streaming: false,
    text: '',
    thinkingText: '',
    steps: [],
    browserSteps: [],
    error: null,
    pendingInterrupt: null,
  });

  useEffect(() => {
    if (!taskId) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setState({
      streaming: true,
      text: '',
      thinkingText: '',
      steps: [],
      browserSteps: [],
      error: null,
      pendingInterrupt: null,
    });

    let seqCounter = 0;

    async function consume() {
      try {
        const res = await fetch(`/stream/${taskId}`, {signal: controller.signal});
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

            if (eventType === 'token' && parsed['source'] === 'main') {
              setState((s) => ({...s, text: s.text + (parsed['text'] as string)}));
            } else if (eventType === 'thinking_token' && parsed['source'] === 'main') {
              setState((s) => ({...s, thinkingText: s.thinkingText + (parsed['text'] as string)}));
            } else if (eventType === 'step') {
              const step: Step = {
                id: String(seqCounter),
                node: parsed['node'] as string,
                source: parsed['source'] as string,
                subagent: (parsed['subagent'] as string | null) ?? null,
                data: parsed['data'] as string | null,
                seq: seqCounter++,
                created_at: new Date().toISOString(),
              };
              setState((s) => ({...s, steps: [...s.steps, step]}));
            } else if (eventType === 'browser_step') {
              const browserStep: BrowserStep = {
                thought: parsed['thought'],
                actions: (parsed['actions'] as unknown[]) ?? [],
                at: new Date().toISOString(),
              };
              setState((s) => ({...s, browserSteps: [...s.browserSteps, browserStep]}));
            } else if (eventType === 'interrupt') {
              setState((s) => ({
                ...s,
                pendingInterrupt: {
                  id: parsed['interrupt_id'] as string,
                  question: parsed['question'] as string,
                },
              }));
            } else if (eventType === 'interrupt_resolved') {
              setState((s) =>
                s.pendingInterrupt?.id === parsed['interrupt_id']
                  ? {...s, pendingInterrupt: null}
                  : s,
              );
            } else if (eventType === 'done' || eventType === 'stopped') {
              setState((s) => ({...s, streaming: false, thinkingText: '', pendingInterrupt: null}));
              await queryClient.invalidateQueries({queryKey: ['conversations']});
              if (conversationId) {
                await queryClient.invalidateQueries({queryKey: ['conversation', conversationId]});
              }
            } else if (eventType === 'error') {
              setState((s) => ({...s, streaming: false, thinkingText: '', pendingInterrupt: null, error: parsed['error'] as string}));
              await queryClient.invalidateQueries({queryKey: ['conversations']});
              if (conversationId) {
                await queryClient.invalidateQueries({queryKey: ['conversation', conversationId]});
              }
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
  }, [taskId]); // eslint-disable-line react-hooks/exhaustive-deps

  return state;
}
