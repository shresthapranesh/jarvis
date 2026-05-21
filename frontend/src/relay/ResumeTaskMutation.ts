import {commitMutation, graphql} from 'react-relay';

import type {ResumeTaskMutation} from '../__generated__/ResumeTaskMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation ResumeTaskMutation($taskId: String!, $answer: String!) {
    resumeTask(taskId: $taskId, answer: $answer)
  }
`;

export function commitResumeTask(taskId: string, answer: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<ResumeTaskMutation>(environment, {
      mutation,
      variables: {taskId, answer},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.resumeTask);
      },
      onError: reject,
    });
  });
}
