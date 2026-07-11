import {commitMutation, graphql} from 'react-relay';

import type {UpdateAutomationMutation} from '../__generated__/UpdateAutomationMutation.graphql';
import type {Automation, CreateAutomationPayload} from '../lib/types';
import {environment} from './environment';
import {mapAutomation} from './AutomationListQuery';
import {payloadToInput} from './automationInput';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation UpdateAutomationMutation($id: ID!, $input: AutomationInput!) {
    updateAutomation(id: $id, input: $input) {
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
      stateful
      conversationId
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

export function commitUpdateAutomation(
  rawId: string,
  payload: CreateAutomationPayload,
): Promise<Automation> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateAutomationMutation>(environment, {
      mutation,
      variables: {
        id: encodeGlobalId('Automation', rawId),
        input: payloadToInput(payload),
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapAutomation(response.updateAutomation));
      },
      onError: reject,
    });
  });
}
