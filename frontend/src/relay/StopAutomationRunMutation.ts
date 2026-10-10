import {commitMutation, graphql} from 'react-relay';

import type {StopAutomationRunMutation} from '../__generated__/StopAutomationRunMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation StopAutomationRunMutation($runId: String!) {
    stopAutomationRun(runId: $runId)
  }
`;

export function commitStopAutomationRun(runId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    commitMutation<StopAutomationRunMutation>(environment, {
      mutation,
      variables: {runId},
      onCompleted: (_response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve();
      },
      onError: reject,
    });
  });
}
