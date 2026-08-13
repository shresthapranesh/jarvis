import {useEffect, useRef} from 'react';

/**
 * Re-fetch a Relay query on an interval.
 *
 * Pair with a `useLazyLoadQuery` on the same query: `refresh` should be one of
 * the `refresh*` helpers in `src/relay/`, which write straight into the Relay
 * store, and the mounted query re-renders off its store subscription. Nothing
 * needs to thread the new data back down.
 *
 * Relay's store only marks a record dirty when a field actually differs, so a
 * poll that returns unchanged data costs a request and no re-render.
 *
 * Pass `null` to stop polling.
 */
export function usePollingRefresh(refresh: () => Promise<unknown>, intervalMs: number | null) {
  // Callers pass inline closures; keep the effect keyed only on the interval.
  const latest = useRef(refresh);
  latest.current = refresh;

  useEffect(() => {
    if (intervalMs === null) return;

    let cancelled = false;
    let busy = false;
    let timer: ReturnType<typeof setTimeout> | null = null;

    // Chained setTimeout rather than setInterval: if a response takes longer
    // than the interval, setInterval would stack overlapping requests.
    const schedule = () => {
      if (cancelled || document.hidden) return;
      timer = setTimeout(run, intervalMs);
    };

    const run = async () => {
      timer = null;
      busy = true;
      try {
        await latest.current();
      } catch {
        // Transient failure — the next tick retries. Errors surface through the
        // mounted query, not here.
      }
      busy = false;
      if (!cancelled) schedule();
    };

    // Matches react-query's refetchIntervalInBackground:false — polling a tab
    // nobody is looking at just burns requests. Catch up on the way back.
    const onVisibilityChange = () => {
      if (document.hidden) {
        if (timer !== null) {
          clearTimeout(timer);
          timer = null;
        }
        return;
      }
      if (timer === null && !busy) void run();
    };

    document.addEventListener('visibilitychange', onVisibilityChange);
    schedule();

    return () => {
      cancelled = true;
      if (timer !== null) clearTimeout(timer);
      document.removeEventListener('visibilitychange', onVisibilityChange);
    };
  }, [intervalMs]);
}
