import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ConversationPageQuery as TQuery} from '../__generated__/ConversationPageQuery.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

export const CONVERSATION_PAGE_SIZE = 10;

// Top-level query: just spreads the refetchable pagination fragment so the
// route loader can warm the Relay store and the component can read it back via
// useLazyLoadQuery + usePaginationFragment.
export const conversationPageQuery = graphql`
  query ConversationPageQuery($id: ID!, $count: Int!, $cursor: String) {
    conversation(id: $id) {
      ...ConversationPageFragment @arguments(count: $count, cursor: $cursor)
    }
  }
`;

export function loadConversationPage(rawId: string): Promise<TQuery['response'] | undefined> {
  return fetchQuery<TQuery>(
    environment,
    conversationPageQuery,
    {
      id: encodeGlobalId('Conversation', rawId),
      count: CONVERSATION_PAGE_SIZE,
      cursor: null,
    },
    {fetchPolicy: 'network-only'},
  ).toPromise();
}
