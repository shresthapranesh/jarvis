import {useCallback, useEffect, useRef, useState} from 'react';

interface Options {
  onSuccess?: () => void;
  onError?: (error: Error) => void;
}

/**
 * Pending/error state around a one-shot async call — the part of react-query's
 * `useMutation` that a normalized Relay store does not replace.
 *
 * Cache consistency belongs in the mutation's own `updater` (see
 * `src/relay/*Mutation.ts`), not here: a write should leave the store correct
 * for every mounted view, not just the component that happened to fire it.
 */
export function useAsyncAction<A extends unknown[]>(
  fn: (...args: A) => Promise<unknown>,
  opts: Options = {},
) {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<Error | null>(null);

  // A dialog usually closes in onSuccess, so the call frequently outlives the
  // component that started it.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Read callbacks off a ref so `run` stays referentially stable even when the
  // caller passes inline closures (they capture form state, so they change on
  // every keystroke).
  const latest = useRef({fn, opts});
  latest.current = {fn, opts};

  const run = useCallback(async (...args: A) => {
    setPending(true);
    setError(null);
    try {
      await latest.current.fn(...args);
      if (mounted.current) setPending(false);
      latest.current.opts.onSuccess?.();
    } catch (e) {
      const err = e instanceof Error ? e : new Error(String(e));
      if (mounted.current) {
        setPending(false);
        setError(err);
      }
      latest.current.opts.onError?.(err);
    }
  }, []);

  const reset = useCallback(() => setError(null), []);

  return {run, pending, error, reset};
}
