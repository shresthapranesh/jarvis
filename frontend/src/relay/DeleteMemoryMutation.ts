import {commitMutation, graphql} from 'react-relay';

import type {DeleteMemoryMutation} from '../__generated__/DeleteMemoryMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation DeleteMemoryMutation($id: String!) {
    deleteMemory(id: $id)
  }
`;

export function commitDeleteMemory(id: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteMemoryMutation>(environment, {
      mutation,
      variables: {id},
      // The payload is a bare Boolean, so the id has to come from the closure.
      // `false` means the server found nothing to delete — leave the store alone
      // rather than dropping a row the backend still has.
      updater: (store, data) => {
        if (!data?.deleteMemory) return;
        const root = store.getRoot();
        const existing = root.getLinkedRecords('memories');
        if (existing) {
          root.setLinkedRecords(
            existing.filter((r) => r && r.getDataID() !== id),
            'memories',
          );
        }
        // MemoryItem.id is the record's data id (Relay's default getDataID
        // reads the `id` field), so the orphaned record can be dropped too.
        store.delete(id);
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.deleteMemory);
      },
      onError: reject,
    });
  });
}
