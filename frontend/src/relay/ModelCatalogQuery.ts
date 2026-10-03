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
      endpoints {
        name
        baseUrl
        hasKey
      }
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

/** An OpenAI-compatible server; its name is the provider prefix of its
 *  models. The key is write-only — only whether one is set comes back. */
export interface ModelEndpoint {
  name: string;
  baseUrl: string;
  hasKey: boolean;
}

export interface ModelCatalogData {
  default: string;
  providers: readonly string[];
  /** Providers that can enumerate their own models — drives the sync picker. */
  discoverableProviders: readonly string[];
  endpoints: readonly ModelEndpoint[];
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
