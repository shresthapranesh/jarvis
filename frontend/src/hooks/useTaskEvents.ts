import {useQueryClient} from '@tanstack/react-query';
import {useEffect, useRef, useState} from 'react';
import {graphql, requestSubscription} from 'react-relay';

import type {useTaskEventsSubscription} from '../__generated__/useTaskEventsSubscription.graphql';
import type {ArtifactRef, Step, TodoItem, TodoStatus} from '../lib/types';
import {refreshArtifactList} from '../relay/ArtifactListQuery';
import {refreshConversationList} from '../relay/ConversationListQuery';
import {loadConversationPage} from '../relay/ConversationPageQuery';
import {refreshDocumentList} from '../relay/DocumentListQuery';
import {environment} from '../relay/environment';
import {refreshTodoList} from '../relay/TodoListQuery';

export interface BrowserStep {
  thought: unknown;
  actions: unknown[];
  at: string;
}

// Live state of one spawned worker, accumulated from worker_* events.
// Keyed by idx (1-based); a later spawn_workers batch in the same turn
// reuses idxs and overwrites the previous batch's cards.
export interface WorkerInfo {
  idx: number;
  role: string;
  task: string;
  status: 'running' | 'done' | 'error';
  // Latest node + step payload (same JSON shape as Step.data) — drives the
  // per-card activity label via describeStep().
  node: string | null;
  stepData: string | null;
  // Rolling tail of the worker's streamed text (last ~800 chars).
  tail: string;
  result: string | null;
}

interface BudgetInfo {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  llmCalls: number;
  toolCalls: number;
}

interface StreamState {
  streaming: boolean;
  text: string;
  thinkingText: string;
  steps: Step[];
  browserSteps: BrowserStep[];
  workers: WorkerInfo[];
  artifacts: ArtifactRef[];
  todos: TodoItem[] | null;
  error: string | null;
  pendingInterrupt: {id: string; question: string} | null;
  budget: BudgetInfo | null;
}

const EMPTY_STATE: StreamState = {
  streaming: false,
  text: '',
  thinkingText: '',
  steps: [],
  browserSteps: [],
  workers: [],
  artifacts: [],
  todos: null,
  error: null,
  pendingInterrupt: null,
  budget: null,
};

function upsertWorker(workers: WorkerInfo[], next: WorkerInfo): WorkerInfo[] {
  const i = workers.findIndex((w) => w.idx === next.idx);
  if (i === -1) return [...workers, next].sort((a, b) => a.idx - b.idx);
  const copy = [...workers];
  copy[i] = next;
  return copy;
}

function patchWorker(
  workers: WorkerInfo[],
  idx: number,
  patch: (w: WorkerInfo) => WorkerInfo,
): WorkerInfo[] {
  const i = workers.findIndex((w) => w.idx === idx);
  if (i === -1) return workers;
  const copy = [...workers];
  copy[i] = patch(copy[i]);
  return copy;
}

// Must match WORKER_RESULT_PERSIST_CAP in core/streaming.py so the live
// mirror of worker_done matches the persisted Step row.
const WORKER_RESULT_CAP = 2000;

function makeWorkerStep(
  seq: number,
  role: string,
  idx: number,
  node: 'worker_start' | 'worker_done',
  data: Record<string, unknown>,
): Step {
  return {
    id: String(seq),
    node,
    source: 'subagent',
    subagent: `${role}:${idx}`,
    data: JSON.stringify(data),
    seq,
    created_at: new Date().toISOString(),
  };
}

const taskEventsSubscription = graphql`
  subscription useTaskEventsSubscription($taskId: String!) {
    taskEvents(taskId: $taskId) {
      __typename
      ... on TokenEvent {
        text
        source
      }
      ... on ThinkingTokenEvent {
        text
        source
      }
      ... on StepEvent {
        node
        source
        subagent
        data
      }
      ... on BrowserStepEvent {
        thought
        actions
        source
      }
      ... on WorkerStartEvent {
        idx
        role
        task
      }
      ... on WorkerStepEvent {
        idx
        role
        node
        data
      }
      ... on WorkerTokenEvent {
        idx
        text
      }
      ... on WorkerDoneEvent {
        idx
        role
        task
        status
        result
      }
      ... on ArtifactEvent {
        artifactId
        title
        action
        preview
      }
      ... on TodosUpdatedEvent {
        todos {
          text
          status
        }
        source
      }
      ... on InterruptEvent {
        interruptId
        question
      }
      ... on InterruptResolvedEvent {
        interruptId
      }
      ... on ApprovalRequestEvent {
        tool
        reason
        args
      }
      ... on ApprovalResolvedEvent {
        tool
        approved
        answer
      }
      ... on WorkflowToolEvent {
        parentRunId
        childEvent
        data
      }
      ... on BudgetExceededEvent {
        reason
        snapshot
      }
      ... on BudgetUpdateEvent {
        inputTokens
        outputTokens
        totalTokens
        llmCalls
        toolCalls
        snapshot
      }
      ... on DoneEvent {
        message
        conversationId
      }
      ... on StoppedEvent {
        message
        conversationId
      }
      ... on ErrorEvent {
        error
      }
    }
  }
`;

export function useTaskEvents(taskId: string | null, conversationId: string | null) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<StreamState>(EMPTY_STATE);

  // Reset state synchronously when taskId changes, so we never render a frame
  // where `text` belongs to the previous task while a new one is starting.
  const [activeTaskId, setActiveTaskId] = useState<string | null>(taskId);
  if (activeTaskId !== taskId) {
    setActiveTaskId(taskId);
    setState(taskId ? {...EMPTY_STATE, streaming: true} : EMPTY_STATE);
  }

  const seqRef = useRef(0);

  useEffect(() => {
    if (!taskId) return;
    seqRef.current = 0;

    const disposable = requestSubscription<useTaskEventsSubscription>(environment, {
      subscription: taskEventsSubscription,
      variables: {taskId},
      onNext: (response) => {
        const evt = response?.taskEvents;
        if (!evt) return;

        switch (evt.__typename) {
          case 'TokenEvent':
            if (evt.source === 'main') {
              setState((s) => ({...s, text: s.text + evt.text}));
            }
            break;
          case 'ThinkingTokenEvent':
            if (evt.source === 'main') {
              setState((s) => ({...s, thinkingText: s.thinkingText + evt.text}));
            }
            break;
          case 'StepEvent': {
            const seq = seqRef.current++;
            const step: Step = {
              id: String(seq),
              node: evt.node,
              source: evt.source,
              subagent: evt.subagent ?? null,
              data: evt.data,
              seq,
              created_at: new Date().toISOString(),
            };
            setState((s) => ({...s, steps: [...s.steps, step]}));
            break;
          }
          case 'BrowserStepEvent': {
            const parsedActions = safeJsonParse(evt.actions);
            const browserStep: BrowserStep = {
              thought: evt.thought,
              actions: Array.isArray(parsedActions) ? parsedActions : [],
              at: new Date().toISOString(),
            };
            setState((s) => ({...s, browserSteps: [...s.browserSteps, browserStep]}));
            break;
          }
          case 'WorkerStartEvent': {
            // Mirror into `steps` with the same shape the backend persists
            // (node/subagent/data), so the grouped sidebar looks identical
            // live and after a reload.
            const step = makeWorkerStep(seqRef.current++, evt.role, evt.idx, 'worker_start', {
              idx: evt.idx,
              role: evt.role,
              task: evt.task,
            });
            setState((s) => ({
              ...s,
              steps: [...s.steps, step],
              workers: upsertWorker(s.workers, {
                idx: evt.idx,
                role: evt.role,
                task: evt.task,
                status: 'running',
                node: null,
                stepData: null,
                tail: '',
                result: null,
              }),
            }));
            break;
          }
          case 'WorkerStepEvent': {
            const step: Step = {
              id: String(seqRef.current),
              node: evt.node,
              source: 'subagent',
              subagent: `${evt.role}:${evt.idx}`,
              data: evt.data,
              seq: seqRef.current++,
              created_at: new Date().toISOString(),
            };
            setState((s) => ({
              ...s,
              steps: [...s.steps, step],
              workers: patchWorker(s.workers, evt.idx, (w) => ({
                ...w,
                node: evt.node,
                stepData: evt.data,
              })),
            }));
            break;
          }
          case 'WorkerTokenEvent':
            setState((s) => ({
              ...s,
              workers: patchWorker(s.workers, evt.idx, (w) => ({
                ...w,
                tail: (w.tail + evt.text).slice(-800),
              })),
            }));
            break;
          case 'WorkerDoneEvent': {
            const step = makeWorkerStep(seqRef.current++, evt.role, evt.idx, 'worker_done', {
              idx: evt.idx,
              role: evt.role,
              task: evt.task,
              status: evt.status,
              result: evt.result.slice(0, WORKER_RESULT_CAP),
            });
            setState((s) => ({
              ...s,
              steps: [...s.steps, step],
              workers: upsertWorker(s.workers, {
                ...(s.workers.find((w) => w.idx === evt.idx) ?? {
                  idx: evt.idx,
                  role: evt.role,
                  task: evt.task,
                  node: null,
                  stepData: null,
                  tail: '',
                }),
                role: evt.role,
                task: evt.task,
                status: evt.status === 'error' ? 'error' : 'done',
                result: evt.result,
              }),
            }));
            break;
          }
          case 'ArtifactEvent': {
            const ref: ArtifactRef = {
              id: evt.artifactId,
              title: evt.title,
              action: (evt.action as 'created' | 'updated') ?? 'created',
              preview: evt.preview ?? undefined,
            };
            setState((s) => ({
              ...s,
              artifacts: [...s.artifacts.filter((a) => a.id !== ref.id), ref],
            }));
            break;
          }
          case 'TodosUpdatedEvent': {
            const todos: TodoItem[] = evt.todos.map((t) => ({
              text: t.text,
              status: t.status as TodoStatus,
            }));
            setState((s) => ({...s, todos}));
            break;
          }
          case 'InterruptEvent':
            setState((s) => ({
              ...s,
              pendingInterrupt: {id: evt.interruptId, question: evt.question},
            }));
            break;
          case 'InterruptResolvedEvent':
            setState((s) =>
              s.pendingInterrupt?.id === evt.interruptId ? {...s, pendingInterrupt: null} : s,
            );
            break;
          case 'ApprovalRequestEvent': {
            // Approval piggy-backs on interrupt mechanism; but if interrupt
            // hasn't arrived yet, show a pending prompt immediately from the
            // approval event. This gives richer UI (tool + reason) while still
            // letting the interrupt be the source of truth for resume.
            const q = `${evt.tool}: ${evt.reason}\n${evt.args ? `Args: ${evt.args}` : ''}`;
            setState((s) => ({
              ...s,
              pendingInterrupt: s.pendingInterrupt ?? {id: `approval-${evt.tool}`, question: q},
            }));
            break;
          }
          case 'ApprovalResolvedEvent':
            // Clear any synthetic approval prompt; interrupt_resolved will also
            // clear the real interrupt id.
            setState((s) => ({...s, pendingInterrupt: null}));
            break;
          case 'WorkflowToolEvent':
            // Treat workflow sub-agent events as steps for now
            break;
          case 'BudgetExceededEvent':
            setState((s) => ({
              ...s,
              error: `Budget exceeded: ${evt.reason}`,
            }));
            break;
          case 'BudgetUpdateEvent':
            setState((s) => ({
              ...s,
              budget: {
                inputTokens: (evt as any).inputTokens,
                outputTokens: (evt as any).outputTokens,
                totalTokens: (evt as any).totalTokens,
                llmCalls: (evt as any).llmCalls,
                toolCalls: (evt as any).toolCalls,
              },
            }));
            break;
          case 'DoneEvent':
          case 'StoppedEvent':
            setState((s) => ({...s, streaming: false, thinkingText: '', pendingInterrupt: null}));
            void (async () => {
              await refreshConversationList();
              await queryClient.invalidateQueries({queryKey: ['running-tasks']});
              if (conversationId) {
                await loadConversationPage(conversationId);
                await refreshArtifactList(conversationId);
                await refreshDocumentList(conversationId);
                await refreshTodoList(conversationId);
              }
            })();
            break;
          case 'ErrorEvent':
            setState((s) => ({
              ...s,
              streaming: false,
              thinkingText: '',
              pendingInterrupt: null,
              error: evt.error,
            }));
            void (async () => {
              await refreshConversationList();
              await queryClient.invalidateQueries({queryKey: ['running-tasks']});
              if (conversationId) {
                await loadConversationPage(conversationId);
                // Re-sync the plan: a crashed run resets todos to [] (or wrote a
                // partial plan), and unlike DoneEvent this branch otherwise left
                // the stale list pinned until a full reload.
                await refreshTodoList(conversationId);
              }
            })();
            break;
        }
      },
      onError: (err) => {
        setState((s) => ({...s, streaming: false, error: err.message}));
      },
      onCompleted: () => {
        setState((s) => ({...s, streaming: false}));
      },
    });

    return () => disposable.dispose();
    // conversationId/queryClient deliberately omitted — they don't drive resubscription
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [taskId]);

  return state;
}

function safeJsonParse(raw: string): unknown {
  try {
    return JSON.parse(raw);
  } catch {
    return null;
  }
}
