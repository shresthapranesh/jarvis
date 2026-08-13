import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {WorkflowRunDetailQuery} from '../__generated__/WorkflowRunDetailQuery.graphql';
import type {WorkflowRun} from '../lib/types';
import {mapWorkflowRun} from './WorkflowRunsQuery';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

export const workflowRunDetailQuery = graphql`
  query WorkflowRunDetailQuery($id: ID!) {
    workflowRun(id: $id) {
      id
      workflowId
      status
      inputs
      outputs
      nodeResults
      error
      startedAt
      finishedAt
    }
  }
`;

export function workflowRunDetailVars(rawId: string) {
  return {id: encodeGlobalId('WorkflowRun', rawId)};
}

export async function fetchWorkflowRun(rawId: string): Promise<WorkflowRun> {
  const data = await fetchQuery<WorkflowRunDetailQuery>(
    environment,
    workflowRunDetailQuery,
    {id: encodeGlobalId('WorkflowRun', rawId)},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  if (!data?.workflowRun) throw new Error('Workflow run not found');
  return mapWorkflowRun(data.workflowRun);
}

export function refreshWorkflowRun(rawId: string) {
  return fetchWorkflowRun(rawId).catch(() => undefined);
}
