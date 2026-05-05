import {useQueryClient} from '@tanstack/react-query';
import {useEffect, useRef, useState} from 'react';

import {refetchConversationFirstPage} from '../lib/api';
import type {ArtifactRef, Step} from '../lib/types';

export interface BrowserStep {
  thought: unknown;
  actions: unknown[];
  at: string;
}

export interface SafetyBlock {
  layer: 'input' | 'output';
  severity?: 'low' | 'medium' | 'high';
  reason?: string;
}

interface StreamState {
  streaming: boolean;
  text: string;
  thinkingText: string;
  steps: Step[];
  browserSteps: BrowserStep[];
  artifacts: ArtifactRef[];
  error: string | null;
  pendingInterrupt: {id: string; question: string} | null;
  safetyBlock: SafetyBlock | null;
}

const EMPTY_STATE: StreamState = {
  streaming: false,
  text: '',
  thinkingText: '',
  steps: [],
  browserSteps: [],
  artifacts: [],
  error: null,
  pendingInterrupt: null,
  safetyBlock: null,
};

export function useStream(taskId: string | null, conversationId: string | null) {
  const queryClient = useQueryClient();
  const abortRef = useRef<AbortController | null>(null);
  const [state, setState] = useState<StreamState>(EMPTY_STATE);

  // Reset state synchronously the moment taskId changes, so we never render
  // a frame where `text` is from the previous task while `runningMsg` already
  // points at a new one.
  const [activeTaskId, setActiveTaskId] = useState<string | null>(taskId);
  if (activeTaskId !== taskId) {
    setActiveTaskId(taskId);
    setState(taskId ? {...EMPTY_STATE, streaming: true} : EMPTY_STATE);
  }

  useEffect(() => {
    if (!taskId) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

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
            } else if (eventType === 'artifact') {
              const ref: ArtifactRef = {
                id: parsed['id'] as string,
                title: parsed['title'] as string,
                action: (parsed['action'] as 'created' | 'updated') ?? 'created',
                preview: parsed['preview'] as string | undefined,
              };
              setState((s) => {
                const others = s.artifacts.filter((a) => a.id !== ref.id);
                return {...s, artifacts: [...others, ref]};
              });
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
            } else if (eventType === 'safety_input_blocked') {
              setState((s) => ({
                ...s,
                safetyBlock: {layer: 'input'},
                text: (parsed['message'] as string) ?? s.text,
              }));
            } else if (eventType === 'safety_output_blocked') {
              setState((s) => ({
                ...s,
                safetyBlock: {
                  layer: 'output',
                  severity: parsed['severity'] as SafetyBlock['severity'],
                  reason: parsed['reason'] as string,
                },
                text: (parsed['redacted_message'] as string) ?? s.text,
              }));
            } else if (eventType === 'done' || eventType === 'stopped') {
              setState((s) => ({...s, streaming: false, thinkingText: '', pendingInterrupt: null}));
              await queryClient.invalidateQueries({queryKey: ['conversations']});
              await queryClient.invalidateQueries({queryKey: ['running-tasks']});
              if (conversationId) {
                await refetchConversationFirstPage(queryClient, conversationId);
                await queryClient.invalidateQueries({queryKey: ['artifacts', conversationId]});
              }
            } else if (eventType === 'error') {
              setState((s) => ({...s, streaming: false, thinkingText: '', pendingInterrupt: null, error: parsed['error'] as string}));
              await queryClient.invalidateQueries({queryKey: ['conversations']});
              await queryClient.invalidateQueries({queryKey: ['running-tasks']});
              if (conversationId) {
                await refetchConversationFirstPage(queryClient, conversationId);
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
