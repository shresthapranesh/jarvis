import {commitMutation, graphql} from 'react-relay';

import type {UpdateMemoryItemMutation} from '../__generated__/UpdateMemoryItemMutation.graphql';
import type {MemoryItem, MemoryKind} from '../lib/types';
import {environment} from './environment';

const mutation = graphql`
  mutation UpdateMemoryItemMutation($id: String!, $text: String!, $kind: String) {
    updateMemoryItem(id: $id, text: $text, kind: $kind) {
      id
      kind
      text
      updatedAt
    }
  }
`;

export function commitUpdateMemoryItem(
  id: string,
  text: string,
  kind?: MemoryKind,
): Promise<MemoryItem> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateMemoryItemMutation>(environment, {
      mutation,
      variables: {id, text, kind: kind ?? null},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        const m = response.updateMemoryItem;
        resolve({id: m.id, kind: m.kind as MemoryKind, text: m.text, updated_at: m.updatedAt});
      },
      onError: reject,
    });
  });
}
