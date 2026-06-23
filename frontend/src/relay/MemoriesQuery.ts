import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {MemoriesQuery} from '../__generated__/MemoriesQuery.graphql';
import type {MemoryItem, MemoryKind} from '../lib/types';
import {environment} from './environment';

export const memoriesQuery = graphql`
  query MemoriesQuery {
    memories {
      id
      kind
      text
      updatedAt
    }
  }
`;

export async function fetchMemories(): Promise<MemoryItem[]> {
  const data = await fetchQuery<MemoriesQuery>(
    environment,
    memoriesQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.memories ?? []).map((m) => ({
    id: m.id,
    kind: m.kind as MemoryKind,
    text: m.text,
    updated_at: m.updatedAt,
  }));
}
