import {commitMutation, graphql} from 'react-relay';

import type {DiscardConversationMutation} from '../__generated__/DiscardConversationMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

// Tears down an incognito conversation on close. The server guards this to
// ephemeral rows, so firing it on a non-incognito conversation is a harmless
// no-op (returns false).
const mutation = graphql`
  mutation DiscardConversationMutation($id: ID!) {
    discardConversation(id: $id)
  }
`;

export function commitDiscardConversation(rawId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DiscardConversationMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Conversation', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.discardConversation);
      },
      onError: reject,
    });
  });
}
