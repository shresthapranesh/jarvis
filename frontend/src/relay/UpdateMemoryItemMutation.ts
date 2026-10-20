import {commitMutation, graphql} from 'react-relay';

import type {UpdateMemoryItemMutation} from '../__generated__/UpdateMemoryItemMutation.graphql';
import type {MemoryItem, MemoryKind} from '../lib/types';
import {environment} from './environment';

const mutation = graphql`
  mutation UpdateMemoryItemMutation($id: String!, $text: String!, $kind: String, $topic: String) {
    updateMemoryItem(id: $id, text: $text, kind: $kind, topic: $topic) {
      id
      kind
      text
      topic
      updatedAt
    }
  }
`;

export function commitUpdateMemoryItem(
  id: string,
  text: string,
  kind?: MemoryKind,
  topic?: string,
): Promise<MemoryItem> {
  return new Promise((resolve, reject) => {
    // No updater: the payload selects the same `id` as MemoriesQuery, so Relay
    // normalizes it onto the existing record and every mounted view of that
    // memory re-renders. Identity, not list membership, is changing.
    commitMutation<UpdateMemoryItemMutation>(environment, {
      mutation,
      variables: {id, text, kind: kind ?? null, topic: topic ?? null},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        const m = response.updateMemoryItem;
        resolve({
          id: m.id,
          kind: m.kind as MemoryKind,
          text: m.text,
          topic: m.topic ?? null,
          updated_at: m.updatedAt,
        });
      },
      onError: reject,
    });
  });
}
