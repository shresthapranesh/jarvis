import {commitMutation, graphql} from 'react-relay';

import type {StopRunningTaskMutation} from '../__generated__/StopRunningTaskMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation StopRunningTaskMutation($taskId: String!) {
    stopRunningTask(taskId: $taskId) {
      ok
      taskId
      kind
    }
  }
`;

export function commitStopRunningTask(taskId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    commitMutation<StopRunningTaskMutation>(environment, {
      mutation,
      variables: {taskId},
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
