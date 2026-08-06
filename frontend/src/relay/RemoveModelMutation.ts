import {commitMutation, graphql} from 'react-relay';

import type {RemoveModelMutation} from '../__generated__/RemoveModelMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation RemoveModelMutation($id: String!) {
    removeModel(id: $id) {
      default
      providers
      available {
        id
        label
        provider
        builtin
      }
    }
  }
`;

export function commitRemoveModel(id: string) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<RemoveModelMutation>(environment, {
      mutation,
      variables: {id},
      onCompleted: (_res, errors) => {
        if (errors && errors.length) {
          reject(new Error(errors.map((e) => e.message).join('; ')));
          return;
        }
        resolve();
      },
      onError: reject,
    });
  });
}
