import {useQueryClient} from '@tanstack/react-query';
import {useEffect, useRef, useState} from 'react';
import type {NodeStatus} from '../lib/types';

export interface WorkflowStreamState {
  streaming: boolean;
  nodeStatuses: Record<string, NodeStatus>;
  outputs: Record<string, unknown> | null;
  error: string | null;
}

export function useWorkflowStream(
  runId: string | null,
  workflowId: string | null,
): WorkflowStreamState {
  const queryClient = useQueryClient();
  const abortRef = useRef<AbortController | null>(null);
  const [state, setState] = useState<WorkflowStreamState>({
    streaming: false,
    nodeStatuses: {},
    outputs: null,
    error: null,
  });

  useEffect(() => {
    if (!runId) return;

    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;

    setState({streaming: true, nodeStatuses: {}, outputs: null, error: null});

    async function consume() {
      try {
        const res = await fetch(`/stream/workflow/${runId}`, {signal: controller.signal});
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

            const nodeId = parsed['node_id'] as string | undefined;

            if (eventType === 'node_start' && nodeId) {
              setState((s) => ({
                ...s,
                nodeStatuses: {
                  ...s.nodeStatuses,
                  [nodeId]: {
                    status: 'running',
                    label: parsed['label'] as string | undefined,
                  },
                },
              }));
            } else if (eventType === 'node_token' && nodeId) {
              const text = parsed['text'] as string;
              setState((s) => {
                const existing = s.nodeStatuses[nodeId] ?? {status: 'running' as const};
                return {
                  ...s,
                  nodeStatuses: {
                    ...s.nodeStatuses,
                    [nodeId]: {
                      ...existing,
                      tokens: (existing.tokens ?? '') + text,
                    },
                  },
                };
              });
            } else if (eventType === 'node_condition' && nodeId) {
              setState((s) => {
                const existing = s.nodeStatuses[nodeId] ?? {status: 'running' as const};
                return {
                  ...s,
                  nodeStatuses: {
                    ...s.nodeStatuses,
                    [nodeId]: {
                      ...existing,
                      verdict: parsed['verdict'] as 'true' | 'false',
                    },
                  },
                };
              });
            } else if (eventType === 'node_done' && nodeId) {
              setState((s) => {
                const existing = s.nodeStatuses[nodeId] ?? {};
                return {
                  ...s,
                  nodeStatuses: {
                    ...s.nodeStatuses,
                    [nodeId]: {
                      ...existing,
                      status: 'done',
                      output: parsed['output'] as Record<string, unknown> | undefined,
                    },
                  },
                };
              });
            } else if (eventType === 'node_error' && nodeId) {
              setState((s) => {
                const existing = s.nodeStatuses[nodeId] ?? {};
                return {
                  ...s,
                  nodeStatuses: {
                    ...s.nodeStatuses,
                    [nodeId]: {
                      ...existing,
                      status: 'error',
                      error: parsed['error'] as string,
                    },
                  },
                };
              });
            } else if (eventType === 'map_start' && nodeId) {
              const total = parsed['total'] as number;
              setState((s) => {
                const existing = s.nodeStatuses[nodeId] ?? {status: 'running' as const};
                return {
                  ...s,
                  nodeStatuses: {
                    ...s.nodeStatuses,
                    [nodeId]: {...existing, mapProgress: {completed: 0, total}},
                  },
                };
              });
            } else if (eventType === 'map_item_done' && nodeId) {
              setState((s) => {
                const existing = s.nodeStatuses[nodeId] ?? {status: 'running' as const};
                const prev = existing.mapProgress ?? {completed: 0, total: 0};
                return {
                  ...s,
                  nodeStatuses: {
                    ...s.nodeStatuses,
                    [nodeId]: {
                      ...existing,
                      mapProgress: {completed: prev.completed + 1, total: prev.total},
                    },
                  },
                };
              });
            } else if (eventType === 'workflow_done') {
              setState((s) => ({
                ...s,
                streaming: false,
                outputs: parsed['outputs'] as Record<string, unknown>,
              }));
              if (workflowId) {
                await queryClient.invalidateQueries({
                  queryKey: ['workflow-runs', workflowId],
                });
              }
            } else if (eventType === 'workflow_error') {
              setState((s) => ({
                ...s,
                streaming: false,
                error: parsed['error'] as string,
              }));
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
