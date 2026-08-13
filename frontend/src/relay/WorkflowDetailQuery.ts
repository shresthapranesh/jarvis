import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {WorkflowDetailQuery} from '../__generated__/WorkflowDetailQuery.graphql';
import type {Workflow} from '../lib/types';
import {mapWorkflow} from './WorkflowListQuery';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

export const workflowDetailQuery = graphql`
  query WorkflowDetailQuery($id: ID!) {
    workflow(id: $id) {
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

export function workflowDetailVars(rawId: string) {
  return {id: encodeGlobalId('Workflow', rawId)};
}

export async function fetchWorkflow(rawId: string): Promise<Workflow> {
  const data = await fetchQuery<WorkflowDetailQuery>(
    environment,
    workflowDetailQuery,
    {id: encodeGlobalId('Workflow', rawId)},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  if (!data?.workflow) throw new Error('Workflow not found');
  return mapWorkflow(data.workflow);
}

export function refreshWorkflow(rawId: string) {
  return fetchWorkflow(rawId).catch(() => undefined);
}
