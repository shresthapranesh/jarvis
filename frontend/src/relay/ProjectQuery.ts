import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ProjectQuery} from '../__generated__/ProjectQuery.graphql';
import type {ProjectDetail} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId, encodeGlobalId} from './globalId';

export const projectQuery = graphql`
  query ProjectQuery($id: ID!) {
    project(id: $id) {
      id
      name
      description
      instructions
      memory
      conversationCount
      createdAt
      updatedAt
      conversations {
        id
        title
        pinned
        messageCount
        createdAt
      }
    }
  }
`;

export async function fetchProject(rawId: string): Promise<ProjectDetail | null> {
  const data = await fetchQuery<ProjectQuery>(
    environment,
    projectQuery,
    {id: encodeGlobalId('Project', rawId)},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  const p = data?.project;
  if (!p) return null;
  return {
    id: decodeGlobalId(p.id),
    name: p.name,
    description: p.description ?? null,
    instructions: p.instructions,
    memory: p.memory,
    conversation_count: p.conversationCount,
    created_at: p.createdAt,
    updated_at: p.updatedAt,
    conversations: p.conversations.map((c) => ({
      id: decodeGlobalId(c.id),
      title: c.title ?? null,
      pinned: c.pinned,
      message_count: c.messageCount,
      created_at: c.createdAt,
    })),
  };
}
