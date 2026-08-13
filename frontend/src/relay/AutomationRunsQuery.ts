import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {AutomationRunsQuery, AutomationRunsQuery$data} from '../__generated__/AutomationRunsQuery.graphql';
import type {AutomationRun, AutomationRunStatus} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId, encodeGlobalId} from './globalId';

export const automationRunsQuery = graphql`
  query AutomationRunsQuery($automationId: ID!) {
    automationRuns(automationId: $automationId) {
      id
      automationId
      status
      triggeredBy
      output
      error
      startedAt
      finishedAt
    }
  }
`;

type RunNode = AutomationRunsQuery$data['automationRuns'][number];

export function mapAutomationRun(r: RunNode): AutomationRun {
  return {
    id: decodeGlobalId(r.id),
    automation_id: r.automationId,
    status: r.status as AutomationRunStatus,
    triggered_by: r.triggeredBy as 'schedule' | 'manual',
    output: r.output ?? null,
    error: r.error ?? null,
    started_at: r.startedAt,
    finished_at: r.finishedAt ?? null,
  };
}

export function automationRunsVars(automationRawId: string) {
  return {automationId: encodeGlobalId('Automation', automationRawId)};
}

export async function fetchAutomationRuns(automationRawId: string): Promise<AutomationRun[]> {
  const data = await fetchQuery<AutomationRunsQuery>(
    environment,
    automationRunsQuery,
    {automationId: encodeGlobalId('Automation', automationRawId)},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.automationRuns ?? []).map(mapAutomationRun);
}

export function refreshAutomationRuns(automationRawId: string) {
  return fetchAutomationRuns(automationRawId).catch(() => undefined);
}
