import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {AgentMemoryQuery} from '../__generated__/AgentMemoryQuery.graphql';
import type {Memory} from '../lib/types';
import {environment} from './environment';

export const agentMemoryQuery = graphql`
  query AgentMemoryQuery {
    agentMemory {
      content
      exists
      modifiedAt
    }
  }
`;

export async function fetchAgentMemory(): Promise<Memory> {
  const data = await fetchQuery<AgentMemoryQuery>(
    environment,
    agentMemoryQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  const m = data?.agentMemory;
  if (!m) return {content: '', exists: false, modified_at: null};
  return {content: m.content, exists: m.exists, modified_at: m.modifiedAt ?? null};
}

export function refreshAgentMemory() {
  return fetchAgentMemory().catch(() => undefined);
}
