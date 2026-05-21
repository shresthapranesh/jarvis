import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {AutomationListQuery, AutomationListQuery$data} from '../__generated__/AutomationListQuery.graphql';
import type {Automation, AutomationInputType, AutomationRunStatus} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId} from './globalId';

export const automationListQuery = graphql`
  query AutomationListQuery {
    automations {
      id
      name
      description
      inputType
      promptText
      model
      codeText
      webhookUrl
      webhookMethod
      webhookHeaders
      webhookBody
      schedule
      enabled
      notifications
      createdAt
      updatedAt
      nextRunAt
      lastRunStatus
      lastRunAt
      successCount7d
      totalCount7d
    }
  }
`;

type AutomationNode = AutomationListQuery$data['automations'][number];

export function mapAutomation(a: AutomationNode): Automation {
  return {
    id: decodeGlobalId(a.id),
    name: a.name,
    description: a.description ?? null,
    input_type: a.inputType as AutomationInputType,
    prompt_text: a.promptText ?? null,
    model: a.model ?? null,
    code_text: a.codeText ?? null,
    webhook_url: a.webhookUrl ?? null,
    webhook_method: a.webhookMethod ?? null,
    webhook_headers: a.webhookHeaders ?? null,
    webhook_body: a.webhookBody ?? null,
    schedule: a.schedule ?? null,
    enabled: a.enabled,
    notifications: a.notifications ?? null,
    created_at: a.createdAt,
    updated_at: a.updatedAt,
    next_run_at: a.nextRunAt ?? null,
    last_run_status: (a.lastRunStatus ?? null) as AutomationRunStatus | null,
    last_run_at: a.lastRunAt ?? null,
    success_count_7d: a.successCount7d ?? undefined,
    total_count_7d: a.totalCount7d ?? undefined,
  };
}

export async function fetchAutomationList(): Promise<Automation[]> {
  const data = await fetchQuery<AutomationListQuery>(
    environment,
    automationListQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.automations ?? []).map(mapAutomation);
}

export function refreshAutomationList() {
  return fetchAutomationList().catch(() => undefined);
}
