import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {BrowserAvailableQuery} from '../__generated__/BrowserAvailableQuery.graphql';
import {environment} from './environment';

export const browserAvailableQuery = graphql`
  query BrowserAvailableQuery {
    browserAvailable
  }
`;

export async function fetchBrowserAvailable(): Promise<boolean> {
  const data = await fetchQuery<BrowserAvailableQuery>(
    environment,
    browserAvailableQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return !!data?.browserAvailable;
}
