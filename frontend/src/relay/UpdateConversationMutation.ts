import {commitMutation, graphql} from 'react-relay';

import type {UpdateConversationMutation, UpdateConversationMutation$data} from '../__generated__/UpdateConversationMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation UpdateConversationMutation(
    $id: ID!
    $title: String
    $model: String
    $pinned: Boolean
    $effort: String
  ) {
    updateConversation(id: $id, title: $title, model: $model, pinned: $pinned, effort: $effort) {
      id
      title
      model
      effort
      pinned
      createdAt
    }
  }
`;

export function commitUpdateConversation(
  rawId: string,
  patch: {
    title?: string | null;
    model?: string | null;
    pinned?: boolean | null;
    effort?: string | null;
  },
): Promise<UpdateConversationMutation$data['updateConversation']> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateConversationMutation>(environment, {
      mutation,
      variables: {
        id: encodeGlobalId('Conversation', rawId),
        title: patch.title ?? null,
        model: patch.model ?? null,
        pinned: patch.pinned ?? null,
        effort: patch.effort ?? null,
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.updateConversation);
      },
      onError: reject,
    });
  });
}
