import {commitMutation, graphql} from 'react-relay';

import type {ConsolidateMemoryMutation} from '../__generated__/ConsolidateMemoryMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation ConsolidateMemoryMutation($model: String) {
    consolidateMemory(model: $model)
  }
`;

export function commitConsolidateMemory(model?: string): Promise<{ok: true; result: string}> {
  return new Promise((resolve, reject) => {
    commitMutation<ConsolidateMemoryMutation>(environment, {
      mutation,
      variables: {model: model ?? null},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve({ok: true, result: response.consolidateMemory});
      },
      onError: reject,
    });
  });
}
