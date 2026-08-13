import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {WorkflowRunsQuery, WorkflowRunsQuery$data} from '../__generated__/WorkflowRunsQuery.graphql';
import type {WorkflowRun, WorkflowRunStatus} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId, encodeGlobalId} from './globalId';

export const workflowRunsQuery = graphql`
  query WorkflowRunsQuery($workflowId: ID!) {
    workflowRuns(workflowId: $workflowId) {
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

type Node = WorkflowRunsQuery$data['workflowRuns'][number];

export function mapWorkflowRun(r: Node): WorkflowRun {
  return {
    id: decodeGlobalId(r.id),
    workflow_id: r.workflowId,
    status: r.status as WorkflowRunStatus,
    inputs: r.inputs ?? null,
    outputs: r.outputs ?? null,
    node_results: r.nodeResults ?? null,
    error: r.error ?? null,
    started_at: r.startedAt,
    finished_at: r.finishedAt ?? null,
  };
}

export function workflowRunsVars(workflowRawId: string) {
  return {workflowId: encodeGlobalId('Workflow', workflowRawId)};
}

export async function fetchWorkflowRuns(workflowRawId: string): Promise<WorkflowRun[]> {
  const data = await fetchQuery<WorkflowRunsQuery>(
    environment,
    workflowRunsQuery,
    {workflowId: encodeGlobalId('Workflow', workflowRawId)},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.workflowRuns ?? []).map(mapWorkflowRun);
}

export function refreshWorkflowRuns(workflowRawId: string) {
  return fetchWorkflowRuns(workflowRawId).catch(() => undefined);
}
