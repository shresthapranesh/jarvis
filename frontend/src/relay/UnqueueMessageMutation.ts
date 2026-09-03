import {commitMutation, graphql} from 'react-relay';

import type {UnqueueMessageMutation} from '../__generated__/UnqueueMessageMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation UnqueueMessageMutation($taskId: String!, $messageId: String!) {
    unqueueMessage(taskId: $taskId, messageId: $messageId)
  }
`;

/** Withdraw a queued message. Resolves false if the run already delivered it. */
export function commitUnqueueMessage(taskId: string, messageId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<UnqueueMessageMutation>(environment, {
      mutation,
      variables: {taskId, messageId},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.unqueueMessage);
      },
      onError: reject,
    });
  });
}
