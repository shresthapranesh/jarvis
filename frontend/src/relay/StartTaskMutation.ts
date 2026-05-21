import {commitMutation, graphql} from 'react-relay';

import type {StartTaskMutation, StartTaskMutation$data, StartTaskMutation$variables} from '../__generated__/StartTaskMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation StartTaskMutation($input: StartTaskInput!) {
    startTask(input: $input) {
      taskId
      conversationId
    }
  }
`;

export function commitStartTask(variables: StartTaskMutation$variables): Promise<StartTaskMutation$data['startTask']> {
  return new Promise((resolve, reject) => {
    commitMutation<StartTaskMutation>(environment, {
      mutation,
      variables,
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.startTask);
      },
      onError: reject,
    });
  });
}
