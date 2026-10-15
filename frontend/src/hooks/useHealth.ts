import {useCallback, useEffect, useState} from 'react';

import {checkHealth} from '../lib/api';

/**
 * Backend reachability for the sidebar status dot.
 *
 * REST, not GraphQL, so there is no Relay store to hang it on. A healthy result
 * is never re-checked — this mirrors the `staleTime: Infinity` it replaces — but
 * an unhealthy one retries when the window regains focus, so the dot recovers
 * on its own once the server is back.
 *
 * `null` until the first check answers, so a page load shows a neutral dot
 * rather than flashing "offline" before the server has had a chance to reply.
 */
export function useHealth(): boolean | null {
  const [healthy, setHealthy] = useState<boolean | null>(null);

  const check = useCallback(async () => {
    try {
      const res = await checkHealth();
      setHealthy(res.status === 'ok');
      return res.status === 'ok';
    } catch {
      setHealthy(false);
      return false;
    }
  }, []);

  useEffect(() => {
    let cancelled = false;
    let healthyNow = false;

    void check().then((ok) => {
      if (!cancelled) healthyNow = ok;
    });

    const onFocus = () => {
      if (healthyNow) return;
      void check().then((ok) => {
        if (!cancelled) healthyNow = ok;
      });
    };
    window.addEventListener('focus', onFocus);
    return () => {
      cancelled = true;
      window.removeEventListener('focus', onFocus);
    };
  }, [check]);

  return healthy;
}
