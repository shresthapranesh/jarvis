import {commitMutation, graphql} from 'react-relay';

import type {ResumeWorkflowRunMutation} from '../__generated__/ResumeWorkflowRunMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation ResumeWorkflowRunMutation($runId: String!, $answer: String!) {
    resumeWorkflowRun(runId: $runId, answer: $answer)
  }
`;

export function commitResumeWorkflowRun(runId: string, answer: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<ResumeWorkflowRunMutation>(environment, {
      mutation,
      variables: {runId, answer},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.resumeWorkflowRun);
      },
      onError: reject,
    });
  });
}
