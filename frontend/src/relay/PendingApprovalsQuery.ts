import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {
  PendingApprovalsQuery,
  PendingApprovalsQuery$data,
} from '../__generated__/PendingApprovalsQuery.graphql';
import type {ApprovalKind, ApprovalSource, PendingApproval} from '../lib/types';
import {environment} from './environment';

export const pendingApprovalsQuery = graphql`
  query PendingApprovalsQuery {
    pendingApprovals {
      id
      source
      kind
      question
      label
      tool
      argsJson
      requestedAt
      deferred
      parentId
      boardTaskId
    }
  }
`;

type Node = PendingApprovalsQuery$data['pendingApprovals'][number];

function mapApproval(a: Node): PendingApproval {
  return {
    id: a.id,
    source: a.source as ApprovalSource,
    kind: a.kind as ApprovalKind,
    question: a.question,
    label: a.label,
    tool: a.tool ?? null,
    args_json: a.argsJson ?? null,
    requested_at: a.requestedAt,
    deferred: a.deferred,
    parent_id: a.parentId ?? null,
    board_task_id: a.boardTaskId ?? null,
  };
}

export async function fetchPendingApprovals(): Promise<PendingApproval[]> {
  const data = await fetchQuery<PendingApprovalsQuery>(
    environment,
    pendingApprovalsQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.pendingApprovals ?? []).map(mapApproval);
}
