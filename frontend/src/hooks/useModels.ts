import {graphql, useLazyLoadQuery} from 'react-relay';

import type {useModelsQuery} from '../__generated__/useModelsQuery.graphql';

export function useModels() {
  const data = useLazyLoadQuery<useModelsQuery>(
    graphql`
      query useModelsQuery {
        models {
          default
          defaultEffort
          effortLevels
          available {
            id
            label
            provider
            efforts
          }
        }
      }
    `,
    {},
    {fetchPolicy: 'store-or-network'},
  );

  return {data: data.models};
}
