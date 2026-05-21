import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {WorkflowListQuery, WorkflowListQuery$data} from '../__generated__/WorkflowListQuery.graphql';
import type {Workflow} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId} from './globalId';

export const workflowListQuery = graphql`
  query WorkflowListQuery {
    workflows {
      id
      name
      description
      definition
      notifications
      createdAt
      updatedAt
    }
  }
`;

type Node = WorkflowListQuery$data['workflows'][number];

export function mapWorkflow(w: Node): Workflow {
  return {
    id: decodeGlobalId(w.id),
    name: w.name,
    description: w.description ?? null,
    definition: w.definition,
    notifications: w.notifications ?? null,
    created_at: w.createdAt,
    updated_at: w.updatedAt,
  };
}

export async function fetchWorkflowList(): Promise<Workflow[]> {
  const data = await fetchQuery<WorkflowListQuery>(
    environment,
    workflowListQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.workflows ?? []).map(mapWorkflow);
}

export function refreshWorkflowList() {
  return fetchWorkflowList().catch(() => undefined);
}
