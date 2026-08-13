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

/**
 * Re-read the list from the network into the store. For writes the store
 * updaters keep mounted views correct on their own; this is for the case they
 * cannot model — consolidation rewrites the whole set server-side, so there is
 * no local delta to apply.
 *
 * Writing to the store is what refreshes the UI: `useLazyLoadQuery` subscribes
 * to the records it read, so a mounted MemoryView re-renders off this fetch
 * without being told about it.
 */
export function refreshMemories() {
  return fetchMemories().catch(() => undefined);
}
