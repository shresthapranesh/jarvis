import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ModelCatalogQuery} from '../__generated__/ModelCatalogQuery.graphql';
import {environment} from './environment';

export const modelCatalogQuery = graphql`
  query ModelCatalogQuery {
    models {
      default
      providers
      discoverableProviders
      available {
        id
        label
        provider
        builtin
        contextWindow
      }
    }
  }
`;

export interface CatalogModel {
  id: string;
  label: string;
  provider: string;
  builtin: boolean;
  contextWindow: number | null;
}

export interface ModelCatalogData {
  default: string;
  providers: readonly string[];
  /** Providers that can enumerate their own models — drives the sync picker. */
  discoverableProviders: readonly string[];
  available: readonly CatalogModel[];
}

export async function fetchModelCatalog(): Promise<ModelCatalogData | undefined> {
  const data = await fetchQuery<ModelCatalogQuery>(
    environment,
    modelCatalogQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return data?.models as ModelCatalogData | undefined;
}

/**
 * The chat model dropdown (`useModels`) reads this same query from the Relay
 * store, so writing the fresh catalog back into the store updates it too — no
 * cross-cache coordination needed.
 */
export function refreshModelCatalog() {
  return fetchModelCatalog().catch(() => undefined);
}
