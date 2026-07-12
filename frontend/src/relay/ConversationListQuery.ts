import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ConversationListQuery} from '../__generated__/ConversationListQuery.graphql';
import {environment} from './environment';

export const conversationListQuery = graphql`
  query ConversationListQuery {
    conversations {
      id
      title
      pinned
    }
  }
`;

export function refreshConversationList() {
  return fetchQuery<ConversationListQuery>(
    environment,
    conversationListQuery,
    {},
    {fetchPolicy: 'network-only'},
  )
    .toPromise()
    .catch(() => undefined);
}
