import {graphql, useLazyLoadQuery} from 'react-relay';

import type {useModelsQuery} from '../__generated__/useModelsQuery.graphql';

export function useModels() {
  const data = useLazyLoadQuery<useModelsQuery>(
    graphql`
      query useModelsQuery {
        models {
          default
          available {
            id
            label
            provider
          }
        }
      }
    `,
    {},
    {fetchPolicy: 'store-or-network'},
  );

  return {data: data.models};
}
