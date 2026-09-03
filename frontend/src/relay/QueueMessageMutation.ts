import {commitMutation, graphql} from 'react-relay';

import type {QueueMessageMutation} from '../__generated__/QueueMessageMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation QueueMessageMutation($taskId: String!, $query: String!) {
    queueMessage(taskId: $taskId, query: $query) {
      messageId
      position
    }
  }
`;

/**
 * Hand a message to a run that is already in flight. The agent delivers it
 * just before its next model call, so it joins the current turn rather than
 * starting a second one — `startTask` here would race the checkpointer.
 */
export function commitQueueMessage(
  taskId: string,
  query: string,
): Promise<{messageId: string; position: number}> {
  return new Promise((resolve, reject) => {
    commitMutation<QueueMessageMutation>(environment, {
      mutation,
      variables: {taskId, query},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.queueMessage);
      },
      onError: reject,
    });
  });
}
