import {commitMutation, graphql} from 'react-relay';

import type {AddModelMutation} from '../__generated__/AddModelMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation AddModelMutation($id: String!, $label: String!, $provider: String) {
    addModel(id: $id, label: $label, provider: $provider) {
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

export function commitAddModel(id: string, label: string, provider?: string | null) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<AddModelMutation>(environment, {
      mutation,
      variables: {id, label, provider: provider ?? null},
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
