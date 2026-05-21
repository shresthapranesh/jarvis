import {commitMutation, graphql} from 'react-relay';

import type {DeleteAutomationMutation} from '../__generated__/DeleteAutomationMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteAutomationMutation($id: ID!) {
    deleteAutomation(id: $id)
  }
`;

export function commitDeleteAutomation(rawId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteAutomationMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Automation', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.deleteAutomation);
      },
      onError: reject,
    });
  });
}
