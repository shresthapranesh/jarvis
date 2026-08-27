import {useSyncExternalStore} from 'react';

/**
 * Must stay in sync with `bp.compact` in theme/tokens.stylex.ts — that
 * breakpoint turns the nav rail into a drawer and reflows the shell; this hook
 * decides what JS does about it (drawer open/closed, whether the rail renders
 * expanded, which affordances are keyboard-only). It cannot read the token:
 * `defineConsts` is inlined at build time into StyleX call sites only.
 */
export const MOBILE_QUERY = '(max-width: 860px)';

function subscribe(cb: () => void) {
  const mq = window.matchMedia(MOBILE_QUERY);
  mq.addEventListener('change', cb);
  return () => mq.removeEventListener('change', cb);
}

export function useIsMobile() {
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(MOBILE_QUERY).matches,
    () => false,
  );
}
