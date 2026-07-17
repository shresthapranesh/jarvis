import {useEffect, useState} from 'react';
import {graphql, requestSubscription} from 'react-relay';

import type {useWorkflowRunEventsSubscription} from '../__generated__/useWorkflowRunEventsSubscription.graphql';
import type {NodeStatus} from '../lib/types';
import {environment} from '../relay/environment';
import {refreshWorkflowRuns} from '../relay/WorkflowRunsQuery';

export interface WorkflowStreamState {
  streaming: boolean;
  nodeStatuses: Record<string, NodeStatus>;
  outputs: Record<string, unknown> | null;
  error: string | null;
  pendingApproval: {nodeId: string; tool: string; reason: string; args: string} | null;
  pendingInterrupt: {id: string; question: string} | null;
}

const subscription = graphql`
  subscription useWorkflowRunEventsSubscription($runId: String!) {
    workflowRunEvents(runId: $runId) {
      __typename
      ... on WorkflowNodeStartEvent {
        nodeId
        nodeType
        label
      }
      ... on WorkflowNodeTokenEvent {
        nodeId
        text
      }
      ... on WorkflowNodeConditionEvent {
        nodeId
        verdict
      }
      ... on WorkflowNodeDoneEvent {
        nodeId
        output
      }
      ... on WorkflowNodeErrorEvent {
        nodeId
        error
      }
      ... on WorkflowMapStartEvent {
        nodeId
        total
      }
      ... on WorkflowMapItemDoneEvent {
        nodeId
        index
        result
      }
      ... on WorkflowApprovalRequestEvent {
        tool
        reason
        args
        nodeId
      }
      ... on WorkflowApprovalResolvedEvent {
        tool
        approved
        answer
        nodeId
      }
      ... on WorkflowInterruptEvent {
        interruptId
        question
      }
      ... on WorkflowInterruptResolvedEvent {
        interruptId
      }
      ... on WorkflowBudgetExceededEvent {
        reason
        snapshot
      }
      ... on WorkflowNodeRetryEvent {
        nodeId
        attempt
        maxRetries
        error
      }
      ... on WorkflowDoneEvent {
        outputs
        runId
      }
      ... on WorkflowStoppedEvent {
        runId
      }
      ... on WorkflowErrorEvent {
        error
        runId
      }
    }
  }
`;

export function useWorkflowRunEvents(
  runId: string | null,
  workflowId: string | null,
): WorkflowStreamState {
  const [state, setState] = useState<WorkflowStreamState>({
    streaming: false,
    nodeStatuses: {},
    outputs: null,
    error: null,
    pendingApproval: null,
    pendingInterrupt: null,
  });

  useEffect(() => {
    if (!runId) return;
    setState({streaming: true, nodeStatuses: {}, outputs: null, error: null, pendingApproval: null, pendingInterrupt: null});

    const disposable = requestSubscription<useWorkflowRunEventsSubscription>(environment, {
      subscription,
      variables: {runId},
      onNext: (response) => {
        const evt = response?.workflowRunEvents;
        if (!evt) return;

        switch (evt.__typename) {
          case 'WorkflowNodeStartEvent':
            setState((s) => ({
              ...s,
              nodeStatuses: {
                ...s.nodeStatuses,
                [evt.nodeId]: {status: 'running', label: evt.label},
              },
            }));
            break;
          case 'WorkflowNodeTokenEvent':
            setState((s) => {
              const existing = s.nodeStatuses[evt.nodeId] ?? {status: 'running' as const};
              return {
                ...s,
                nodeStatuses: {
                  ...s.nodeStatuses,
                  [evt.nodeId]: {...existing, tokens: (existing.tokens ?? '') + evt.text},
                },
              };
            });
            break;
          case 'WorkflowNodeConditionEvent':
            setState((s) => {
              const existing = s.nodeStatuses[evt.nodeId] ?? {status: 'running' as const};
              return {
                ...s,
                nodeStatuses: {
                  ...s.nodeStatuses,
                  [evt.nodeId]: {...existing, verdict: evt.verdict as 'true' | 'false'},
                },
              };
            });
            break;
          case 'WorkflowNodeDoneEvent':
            setState((s) => {
              const existing = s.nodeStatuses[evt.nodeId] ?? {};
              return {
                ...s,
                nodeStatuses: {
                  ...s.nodeStatuses,
                  [evt.nodeId]: {
                    ...existing,
                    status: 'done',
                    output: evt.output as Record<string, unknown> | undefined,
                  },
                },
              };
            });
            break;
          case 'WorkflowNodeErrorEvent':
            setState((s) => {
              const existing = s.nodeStatuses[evt.nodeId] ?? {};
              return {
                ...s,
                nodeStatuses: {
                  ...s.nodeStatuses,
                  [evt.nodeId]: {...existing, status: 'error', error: evt.error},
                },
              };
            });
            break;
          case 'WorkflowMapStartEvent':
            setState((s) => {
              const existing = s.nodeStatuses[evt.nodeId] ?? {status: 'running' as const};
              return {
                ...s,
                nodeStatuses: {
                  ...s.nodeStatuses,
                  [evt.nodeId]: {...existing, mapProgress: {completed: 0, total: evt.total}},
                },
              };
            });
            break;
          case 'WorkflowMapItemDoneEvent':
            setState((s) => {
              const existing = s.nodeStatuses[evt.nodeId] ?? {status: 'running' as const};
              const prev = existing.mapProgress ?? {completed: 0, total: 0};
              return {
                ...s,
                nodeStatuses: {
                  ...s.nodeStatuses,
                  [evt.nodeId]: {
                    ...existing,
                    mapProgress: {completed: prev.completed + 1, total: prev.total},
                  },
                },
              };
            });
            break;
          case 'WorkflowApprovalRequestEvent':
            setState((s) => ({
              ...s,
              pendingApproval: {
                nodeId: evt.nodeId ?? evt.tool,
                tool: evt.tool,
                reason: evt.reason,
                args: evt.args,
              },
            }));
            break;
          case 'WorkflowApprovalResolvedEvent':
            setState((s) => ({...s, pendingApproval: null}));
            break;
          case 'WorkflowInterruptEvent':
            setState((s) => ({
              ...s,
              pendingInterrupt: {id: evt.interruptId, question: evt.question},
            }));
            break;
          case 'WorkflowInterruptResolvedEvent':
            setState((s) => ({...s, pendingInterrupt: null}));
            break;
          case 'WorkflowBudgetExceededEvent':
            setState((s) => ({...s, error: `Budget exceeded: ${evt.reason}`}));
            break;
          case 'WorkflowNodeRetryEvent':
            setState((s) => {
              const existing = s.nodeStatuses[evt.nodeId] ?? {status: 'running' as const};
              return {
                ...s,
                nodeStatuses: {
                  ...s.nodeStatuses,
                  [evt.nodeId]: {
                    ...existing,
                    status: 'running',
                    error: `Retry ${evt.attempt}/${evt.maxRetries}: ${evt.error}`,
                  },
                },
              };
            });
            break;
          case 'WorkflowDoneEvent':
            setState((s) => ({
              ...s,
              streaming: false,
              outputs: evt.outputs as Record<string, unknown>,
              pendingApproval: null,
              pendingInterrupt: null,
            }));
            if (workflowId) void refreshWorkflowRuns(workflowId);
            break;
          case 'WorkflowStoppedEvent':
            setState((s) => ({...s, streaming: false, pendingApproval: null, pendingInterrupt: null}));
            if (workflowId) void refreshWorkflowRuns(workflowId);
            break;
          case 'WorkflowErrorEvent':
            setState((s) => ({...s, streaming: false, error: evt.error, pendingApproval: null, pendingInterrupt: null}));
            if (workflowId) void refreshWorkflowRuns(workflowId);
            break;
        }
      },
      onError: (err) => setState((s) => ({...s, streaming: false, error: err.message})),
      onCompleted: () => setState((s) => ({...s, streaming: false})),
    });

    return () => disposable.dispose();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  return state;
}
