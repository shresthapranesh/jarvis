import {commitMutation, graphql} from 'react-relay';

import type {AddMemoryMutation} from '../__generated__/AddMemoryMutation.graphql';
import type {MemoryItem, MemoryKind} from '../lib/types';
import {environment} from './environment';

const mutation = graphql`
  mutation AddMemoryMutation($text: String!, $kind: String!) {
    addMemory(text: $text, kind: $kind) {
      id
      kind
      text
      updatedAt
    }
  }
`;

export function commitAddMemory(text: string, kind: MemoryKind): Promise<MemoryItem> {
  return new Promise((resolve, reject) => {
    commitMutation<AddMemoryMutation>(environment, {
      mutation,
      variables: {text, kind},
      // `memories` is a plain list, not a @connection, so Relay has no
      // declarative directive for this — the new node is normalized into the
      // store automatically, but nothing links it into the root list.
      updater: (store) => {
        const created = store.getRootField('addMemory');
        if (!created) return;
        const root = store.getRoot();
        // The query selects `memories` with no arguments, so the storage key is
        // the bare field name.
        const existing = root.getLinkedRecords('memories') ?? [];
        if (existing.some((r) => r?.getDataID() === created.getDataID())) return;
        root.setLinkedRecords([...existing, created], 'memories');
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        const m = response.addMemory;
        resolve({id: m.id, kind: m.kind as MemoryKind, text: m.text, updated_at: m.updatedAt});
      },
      onError: reject,
    });
  });
}
