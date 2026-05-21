import {commitMutation, graphql} from 'react-relay';

import type {DeleteNotificationChannelMutation} from '../__generated__/DeleteNotificationChannelMutation.graphql';
import type {NotificationChannelReference} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteNotificationChannelMutation($id: ID!) {
    deleteNotificationChannel(id: $id)
  }
`;

export interface DeleteNotificationChannelResult {
  ok: boolean;
  references?: NotificationChannelReference[];
}

// Backend rejects in-use deletes with `channel in use by N reference(s): kind:name, …`.
// Parse it back into the structured shape the UI expects.
function parseInUseRefs(message: string): NotificationChannelReference[] | null {
  const m = message.match(/channel in use by \d+ reference\(s\): (.+)$/);
  if (!m) return null;
  return m[1]
    .split(', ')
    .map((part) => {
      const colon = part.indexOf(':');
      if (colon < 0) return null;
      const kind = part.slice(0, colon);
      if (kind !== 'automation' && kind !== 'workflow') return null;
      return {kind, id: '', name: part.slice(colon + 1)};
    })
    .filter((x): x is NotificationChannelReference => x !== null);
}

export function commitDeleteNotificationChannel(
  rawId: string,
): Promise<DeleteNotificationChannelResult> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteNotificationChannelMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('NotificationChannel', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          const msg = errors[0].message;
          const refs = parseInUseRefs(msg);
          if (refs) {
            resolve({ok: false, references: refs});
            return;
          }
          reject(new Error(msg));
          return;
        }
        resolve({ok: response.deleteNotificationChannel});
      },
      onError: (err) => {
        const refs = parseInUseRefs(err.message);
        if (refs) {
          resolve({ok: false, references: refs});
          return;
        }
        reject(err);
      },
    });
  });
}
