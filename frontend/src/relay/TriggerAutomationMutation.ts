import {commitMutation, graphql} from 'react-relay';

import type {TriggerAutomationMutation} from '../__generated__/TriggerAutomationMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation TriggerAutomationMutation($id: ID!) {
    triggerAutomation(id: $id)
  }
`;

export function commitTriggerAutomation(rawId: string): Promise<{run_id: string}> {
  return new Promise((resolve, reject) => {
    commitMutation<TriggerAutomationMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Automation', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve({run_id: response.triggerAutomation});
      },
      onError: reject,
    });
  });
}
