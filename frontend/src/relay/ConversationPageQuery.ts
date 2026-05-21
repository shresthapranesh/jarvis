import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ConversationPageQuery} from '../__generated__/ConversationPageQuery.graphql';
import type {MessagePage} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId, encodeGlobalId} from './globalId';

const CONVERSATION_PAGE_SIZE = 10;

const conversationPageQuery = graphql`
  query ConversationPageQuery($id: ID!, $limit: Int!, $before: DateTime) {
    conversation(id: $id) {
      id
      title
      model
      createdAt
      messages(limit: $limit, before: $before) {
        hasMore
        messages {
          id
          role
          content
          model
          status
          createdAt
          steps {
            id
            node
            source
            data
            seq
            createdAt
          }
        }
      }
    }
  }
`;

export async function fetchConversationPage(
  rawId: string,
  before?: string,
  limit: number = CONVERSATION_PAGE_SIZE,
): Promise<MessagePage> {
  const data = await fetchQuery<ConversationPageQuery>(
    environment,
    conversationPageQuery,
    {
      id: encodeGlobalId('Conversation', rawId),
      limit,
      before: before ?? null,
    },
    {fetchPolicy: 'network-only'},
  ).toPromise();
  const conv = data?.conversation;
  if (!conv) throw new Error('Conversation not found');
  return {
    id: decodeGlobalId(conv.id),
    title: conv.title ?? null,
    model: conv.model,
    created_at: conv.createdAt,
    has_more: conv.messages.hasMore,
    messages: conv.messages.messages.map((m) => ({
      id: decodeGlobalId(m.id),
      role: m.role as 'user' | 'assistant',
      content: m.content,
      model: m.model ?? null,
      status: m.status,
      created_at: m.createdAt,
      steps: m.steps.map((s) => ({
        id: s.id,
        node: s.node,
        source: s.source,
        data: s.data ?? null,
        seq: s.seq,
        created_at: s.createdAt,
      })),
    })),
  };
}
