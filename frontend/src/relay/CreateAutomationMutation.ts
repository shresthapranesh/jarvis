import {commitMutation, graphql} from 'react-relay';

import type {CreateAutomationMutation} from '../__generated__/CreateAutomationMutation.graphql';
import type {Automation, CreateAutomationPayload} from '../lib/types';
import {environment} from './environment';
import {mapAutomation} from './AutomationListQuery';
import {payloadToInput} from './automationInput';

const mutation = graphql`
  mutation CreateAutomationMutation($input: AutomationInput!) {
    createAutomation(input: $input) {
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

export function commitCreateAutomation(payload: CreateAutomationPayload): Promise<Automation> {
  return new Promise((resolve, reject) => {
    commitMutation<CreateAutomationMutation>(environment, {
      mutation,
      variables: {input: payloadToInput(payload)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapAutomation(response.createAutomation));
      },
      onError: reject,
    });
  });
}
