import {commitMutation, graphql} from 'react-relay';

import type {UpdateMemoryMutation} from '../__generated__/UpdateMemoryMutation.graphql';
import type {Memory} from '../lib/types';
import {environment} from './environment';

const mutation = graphql`
  mutation UpdateMemoryMutation($content: String!) {
    updateMemory(content: $content) {
      content
      exists
      modifiedAt
    }
  }
`;

export function commitUpdateMemory(content: string): Promise<Memory> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateMemoryMutation>(environment, {
      mutation,
      variables: {content},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        const m = response.updateMemory;
        resolve({content: m.content, exists: m.exists, modified_at: m.modifiedAt ?? null});
      },
      onError: reject,
    });
  });
}
