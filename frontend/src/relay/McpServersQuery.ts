import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';
import type {McpServersQuery} from '../__generated__/McpServersQuery.graphql';
import {environment} from './environment';

export const mcpServersQuery = graphql`
  query McpServersQuery {
    mcpServers {
      name
      config
      transport
      command
      url
      toolCount
      enabled
      loadMode
      tools
    }
  }
`;

export async function fetchMcpServers() {
  const data = await fetchQuery<McpServersQuery>(environment, mcpServersQuery, {}, {fetchPolicy: 'network-only'}).toPromise();
  return {
    servers: data?.mcpServers ?? [],
  };
}

export function refreshMcpServers() {
  return fetchMcpServers().catch(() => undefined);
}
