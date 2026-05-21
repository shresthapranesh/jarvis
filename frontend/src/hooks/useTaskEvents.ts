import {useQueryClient} from '@tanstack/react-query';
import {useEffect, useRef, useState} from 'react';
import {graphql, requestSubscription} from 'react-relay';

import type {useTaskEventsSubscription} from '../__generated__/useTaskEventsSubscription.graphql';
import {refetchConversationFirstPage} from '../lib/api';
import type {ArtifactRef, Step, TodoItem, TodoStatus} from '../lib/types';
import {refreshArtifactList} from '../relay/ArtifactListQuery';
import {refreshConversationList} from '../relay/ConversationListQuery';
import {refreshDocumentList} from '../relay/DocumentListQuery';
import {environment} from '../relay/environment';
import {refreshTodoList} from '../relay/TodoListQuery';

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
  todos: TodoItem[] | null;
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
  todos: null,
  error: null,
  pendingInterrupt: null,
  safetyBlock: null,
};

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
      ... on SafetyInputBlockedEvent {
        message
      }
      ... on SafetyOutputBlockedEvent {
        severity
        reason
        redactedMessage
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
          case 'SafetyInputBlockedEvent':
            setState((s) => ({...s, safetyBlock: {layer: 'input'}, text: evt.message}));
            break;
          case 'SafetyOutputBlockedEvent':
            setState((s) => ({
              ...s,
              safetyBlock: {
                layer: 'output',
                severity: evt.severity as SafetyBlock['severity'],
                reason: evt.reason,
              },
              text: evt.redactedMessage,
            }));
            break;
          case 'DoneEvent':
          case 'StoppedEvent':
            setState((s) => ({...s, streaming: false, thinkingText: '', pendingInterrupt: null}));
            void (async () => {
              await refreshConversationList();
              await queryClient.invalidateQueries({queryKey: ['running-tasks']});
              if (conversationId) {
                await refetchConversationFirstPage(queryClient, conversationId);
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
                await refetchConversationFirstPage(queryClient, conversationId);
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
