import {commitMutation, graphql} from 'react-relay';

import type {SetDefaultModelMutation} from '../__generated__/SetDefaultModelMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation SetDefaultModelMutation($id: String!) {
    setDefaultModel(id: $id) {
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

export function commitSetDefaultModel(id: string) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<SetDefaultModelMutation>(environment, {
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
