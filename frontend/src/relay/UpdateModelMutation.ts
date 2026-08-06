import {commitMutation, graphql} from 'react-relay';

import type {UpdateModelMutation} from '../__generated__/UpdateModelMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation UpdateModelMutation($id: String!, $label: String!, $provider: String) {
    updateModel(id: $id, label: $label, provider: $provider) {
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

export function commitUpdateModel(id: string, label: string, provider?: string | null) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<UpdateModelMutation>(environment, {
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
