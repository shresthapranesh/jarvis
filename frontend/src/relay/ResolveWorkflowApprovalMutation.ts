import {commitMutation, graphql} from 'react-relay';

import type {ResolveWorkflowApprovalMutation} from '../__generated__/ResolveWorkflowApprovalMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation ResolveWorkflowApprovalMutation($runId: String!, $approved: Boolean!, $answer: String) {
    resolveWorkflowApproval(runId: $runId, approved: $approved, answer: $answer)
  }
`;

export function commitResolveWorkflowApproval(runId: string, approved: boolean, answer?: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<ResolveWorkflowApprovalMutation>(environment, {
      mutation,
      variables: {runId, approved, answer: answer ?? null},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.resolveWorkflowApproval);
      },
      onError: reject,
    });
  });
}
