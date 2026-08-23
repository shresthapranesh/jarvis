import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ModelSyncQuery as TModelSyncQuery} from '../__generated__/ModelSyncQuery.graphql';
import {environment} from './environment';

/**
 * `model sync` over GraphQL — the catalog-vs-provider drift report.
 *
 * Every `id` is aliased to `modelId`. Relay's default `getDataID` keys a record
 * by its `id` field alone, ignoring the typename, so a `DiscoveredModel` or
 * `WindowFinding` selecting `id` would normalize onto the *same store record*
 * as the `ModelSpec` with that id and overwrite its label/provider with report
 * data. Aliasing keeps these read-only report rows in their own client records.
 */
export const modelSyncQuery = graphql`
  query ModelSyncQuery($provider: String, $probe: Boolean!) {
    modelSync(provider: $provider, probe: $probe) {
      provider
      offered
      skipped
      probed
      clean
      missing
      unreachable {
        modelId: id
        reason
      }
      windows {
        modelId: id
        label
        provider
        catalogWindow
        providerWindow
        builtin
      }
      newModels {
        modelId: id
        label
        provider
        contextWindow
        description
        likelyChat
      }
    }
  }
`;

export interface DiscoveredModelRow {
  modelId: string;
  label: string;
  provider: string;
  contextWindow: number | null;
  description: string | null;
  likelyChat: boolean;
}

export interface WindowFindingRow {
  modelId: string;
  label: string;
  provider: string;
  catalogWindow: number | null;
  providerWindow: number;
  builtin: boolean;
}

export interface SyncReport {
  provider: string;
  offered: number;
  skipped: string | null;
  probed: boolean;
  clean: boolean;
  missing: readonly string[];
  unreachable: readonly {modelId: string; reason: string}[];
  windows: readonly WindowFindingRow[];
  newModels: readonly DiscoveredModelRow[];
}

export async function fetchModelSync(
  provider: string | null,
  probe: boolean,
): Promise<readonly SyncReport[]> {
  const data = await fetchQuery<TModelSyncQuery>(
    environment,
    modelSyncQuery,
    {provider, probe},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.modelSync ?? []) as readonly SyncReport[];
}
