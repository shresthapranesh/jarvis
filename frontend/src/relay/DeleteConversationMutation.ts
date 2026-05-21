import {commitMutation, graphql} from 'react-relay';

import type {DeleteConversationMutation} from '../__generated__/DeleteConversationMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteConversationMutation($id: ID!) {
    deleteConversation(id: $id)
  }
`;

export function commitDeleteConversation(rawId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteConversationMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Conversation', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.deleteConversation);
      },
      onError: reject,
    });
  });
}
