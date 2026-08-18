import {commitMutation, graphql} from 'react-relay';

import type {ResolveApprovalMutation} from '../__generated__/ResolveApprovalMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation ResolveApprovalMutation($id: String!, $answer: String!) {
    resolveApproval(id: $id, answer: $answer) {
      id
      status
      result
    }
  }
`;

export interface ResolvedApproval {
  status: string;
  result: string | null;
}

/**
 * Answer one approval, whatever shape it is.
 *
 * One mutation rather than four: the server dispatches on the stored row, so
 * the inbox does not need to know whether answering means executing a delete,
 * waking a suspended run, or re-queuing a board task.
 */
export function commitResolveApproval(id: string, answer: string): Promise<ResolvedApproval> {
  return new Promise((resolve, reject) => {
    commitMutation<ResolveApprovalMutation>(environment, {
      mutation,
      variables: {id, answer},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve({
          status: response.resolveApproval.status,
          result: response.resolveApproval.result ?? null,
        });
      },
      onError: reject,
    });
  });
}
