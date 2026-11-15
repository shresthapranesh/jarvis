import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ConversationTaskQuery} from '../__generated__/ConversationTaskQuery.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

/**
 * The board task behind a task's conversation. Asked on its own, and only for
 * a task's conversation, rather than with every conversation page.
 */
export const conversationTaskQuery = graphql`
  query ConversationTaskQuery($id: ID!) {
    conversation(id: $id) {
      id
      boardTask {
        id
        status
        blockedKind
        blockedReason
        runId
      }
    }
  }
`;

/** Read it from the server; the mounted query re-renders off the store. */
export async function fetchConversationTask(rawId: string) {
  const data = await fetchQuery<ConversationTaskQuery>(
    environment,
    conversationTaskQuery,
    {id: encodeGlobalId('Conversation', rawId)},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return data?.conversation?.boardTask ?? null;
}
