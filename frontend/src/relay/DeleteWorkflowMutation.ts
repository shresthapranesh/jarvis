import {commitMutation, graphql} from 'react-relay';

import type {DeleteWorkflowMutation} from '../__generated__/DeleteWorkflowMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteWorkflowMutation($id: ID!) {
    deleteWorkflow(id: $id)
  }
`;

export function commitDeleteWorkflow(rawId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteWorkflowMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Workflow', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.deleteWorkflow);
      },
      onError: reject,
    });
  });
}
