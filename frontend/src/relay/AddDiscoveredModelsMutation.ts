import {commitMutation, graphql} from 'react-relay';

import type {AddDiscoveredModelsMutation} from '../__generated__/AddDiscoveredModelsMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation AddDiscoveredModelsMutation($models: [DiscoveredModelInput!]!) {
    addDiscoveredModels(models: $models) {
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

export interface DiscoveredModelDraft {
  id: string;
  label: string;
  provider?: string | null;
  contextWindow?: number | null;
}

/**
 * Register models found by `modelSync` into the custom layer. Also the apply
 * path for a context_window finding — the backend upserts by id, so re-sending
 * an existing custom model with the provider's window corrects it.
 */
export function commitAddDiscoveredModels(models: readonly DiscoveredModelDraft[]) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<AddDiscoveredModelsMutation>(environment, {
      mutation,
      variables: {
        models: models.map((m) => ({
          id: m.id,
          label: m.label,
          provider: m.provider ?? null,
          contextWindow: m.contextWindow ?? null,
        })),
      },
      onCompleted: (_res, errors) => {
        if (errors && errors.length) {
          reject(new Error(errors.map((e) => e.message).join('; ')));
          return;
        }
        resolve();
      },
      onError: reject,
    });
  });
}
