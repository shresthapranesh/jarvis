import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {BoardTasksQuery, BoardTasksQuery$data} from '../__generated__/BoardTasksQuery.graphql';
import type {BoardTask, BoardTaskStatus} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId} from './globalId';

export const boardTasksQuery = graphql`
  query BoardTasksQuery($includeArchived: Boolean!) {
    boardTasks(includeArchived: $includeArchived) {
      id
      title
      body
      status
      priority
      createdBy
      model
      skill
      blockedReason
      blockedKind
      failureCount
      summary
      resultMetadata
      conversationId
      runId
      parentIds
      childIds
      createdAt
      updatedAt
      startedAt
      finishedAt
    }
  }
`;

type BoardTaskNode = BoardTasksQuery$data['boardTasks'][number];

export function mapBoardTask(t: BoardTaskNode): BoardTask {
  return {
    id: decodeGlobalId(t.id),
    title: t.title,
    body: t.body ?? null,
    status: t.status as BoardTaskStatus,
    priority: t.priority,
    created_by: t.createdBy as 'user' | 'agent',
    model: t.model ?? null,
    skill: t.skill ?? null,
    blocked_reason: t.blockedReason ?? null,
    blocked_kind: t.blockedKind ?? null,
    failure_count: t.failureCount,
    summary: t.summary ?? null,
    result_metadata: t.resultMetadata ?? null,
    conversation_id: t.conversationId,
    run_id: t.runId ?? null,
    parent_ids: [...t.parentIds],
    child_ids: [...t.childIds],
    created_at: t.createdAt,
    updated_at: t.updatedAt,
    started_at: t.startedAt ?? null,
    finished_at: t.finishedAt ?? null,
  };
}

export async function fetchBoardTasks(includeArchived = false): Promise<BoardTask[]> {
  const data = await fetchQuery<BoardTasksQuery>(
    environment,
    boardTasksQuery,
    {includeArchived},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.boardTasks ?? []).map(mapBoardTask);
}
