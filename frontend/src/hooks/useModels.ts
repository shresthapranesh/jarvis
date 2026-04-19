import {useQuery} from '@tanstack/react-query';

import {fetchModels} from '../lib/api';

/**
 * Fetch the backend-defined model catalog once per session.
 *
 * The catalog is the single source of truth for which models the UI may
 * offer — it's defined in agents.py and served by GET /models. Every
 * model-picker component in the app consumes this hook so there's no
 * drift between selectors and no hardcoded model lists in TSX.
 *
 * `staleTime: Infinity` because the list only changes on a server restart.
 */
export function useModels() {
  return useQuery({
    queryKey: ['models'],
    queryFn: fetchModels,
    staleTime: Infinity,
    refetchOnWindowFocus: false,
  });
}
