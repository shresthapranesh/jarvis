import {commitMutation, graphql} from 'react-relay';

import type {StopTaskMutation} from '../__generated__/StopTaskMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation StopTaskMutation($taskId: String!) {
    stopTask(taskId: $taskId)
  }
`;

export function commitStopTask(taskId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<StopTaskMutation>(environment, {
      mutation,
      variables: {taskId},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.stopTask);
      },
      onError: reject,
    });
  });
}
