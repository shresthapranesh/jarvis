import {commitMutation, graphql} from 'react-relay';

import type {DeleteAgentMemoryMutation} from '../__generated__/DeleteAgentMemoryMutation.graphql';
import type {Memory} from '../lib/types';
import {environment} from './environment';

// `main.py memory reset` — deletes the blob entry outright. Distinct from
// updateMemory(""), which leaves an empty-but-present entry; the agent's
// fallback branches on `exists`, so the two are not the same state.
const mutation = graphql`
  mutation DeleteAgentMemoryMutation {
    deleteAgentMemory {
      content
      exists
      modifiedAt
    }
  }
`;

export function commitDeleteAgentMemory(): Promise<Memory> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteAgentMemoryMutation>(environment, {
      mutation,
      variables: {},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        const m = response.deleteAgentMemory;
        resolve({content: m.content, exists: m.exists, modified_at: m.modifiedAt ?? null});
      },
      onError: reject,
    });
  });
}
