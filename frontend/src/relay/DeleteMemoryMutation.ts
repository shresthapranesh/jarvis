import {commitMutation, graphql} from 'react-relay';

import type {DeleteMemoryMutation} from '../__generated__/DeleteMemoryMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation DeleteMemoryMutation($id: String!) {
    deleteMemory(id: $id)
  }
`;

export function commitDeleteMemory(id: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteMemoryMutation>(environment, {
      mutation,
      variables: {id},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.deleteMemory);
      },
      onError: reject,
    });
  });
}
