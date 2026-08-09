import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ConversationListQuery} from '../__generated__/ConversationListQuery.graphql';
import {environment} from './environment';
import {decodeGlobalId} from './globalId';

export const conversationListQuery = graphql`
  query ConversationListQuery {
    conversations {
      id
      title
      pinned
      projectId
      createdAt
      messageCount
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

// Decoded flat list — used by the project "add existing conversation" picker.
export interface ConversationListItem {
  id: string;
  title: string | null;
  pinned: boolean;
  project_id: string | null;
}

export async function fetchConversationList(): Promise<ConversationListItem[]> {
  const data = await refreshConversationList();
  return (data?.conversations ?? []).map((c) => ({
    id: decodeGlobalId(c.id),
    title: c.title ?? null,
    pinned: c.pinned,
    project_id: c.projectId ?? null,
  }));
}
