import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {
  RunningTasksQuery,
  RunningTasksQuery$data,
} from '../__generated__/RunningTasksQuery.graphql';
import type {RunningTask, TaskKind} from '../lib/types';
import {environment} from './environment';

export const runningTasksQuery = graphql`
  query RunningTasksQuery {
    runningTasks {
      id
      kind
      label
      parentId
      startedAt
      hasInterrupt
      cancelled
      done
      inputTokens
      outputTokens
      totalTokens
      llmCalls
      toolCalls
      budgetExceeded
      budgetReason
    }
  }
`;

type Node = RunningTasksQuery$data['runningTasks'][number];

function mapTask(t: Node): RunningTask {
  return {
    id: t.id,
    kind: t.kind as TaskKind,
    label: t.label,
    parent_id: t.parentId ?? null,
    started_at: t.startedAt,
    has_interrupt: t.hasInterrupt,
    cancelled: t.cancelled,
    done: t.done,
    input_tokens: (t as any).inputTokens ?? 0,
    output_tokens: (t as any).outputTokens ?? 0,
    total_tokens: (t as any).totalTokens ?? 0,
    llm_calls: (t as any).llmCalls ?? 0,
    tool_calls: (t as any).toolCalls ?? 0,
    budget_exceeded: !!(t as any).budgetExceeded,
    budget_reason: (t as any).budgetReason ?? null,
  };
}

export async function fetchRunningTasks(): Promise<RunningTask[]> {
  const data = await fetchQuery<RunningTasksQuery>(
    environment,
    runningTasksQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.runningTasks ?? []).map(mapTask);
}

export function refreshRunningTasks() {
  return fetchRunningTasks().catch(() => undefined);
}
