import {useEffect, useSyncExternalStore} from 'react';

/**
 * The name of the thing the current page shows — a conversation, a workflow —
 * for the browser tab's title.
 *
 * The shell sets the title, because it also knows the section and what is
 * waiting on the user; a page only knows its own name, and tells the shell
 * here. One name at a time: the page that set it clears it on the way out, so
 * a page with no name of its own falls back to its section.
 */

let current: string | null = null;
const listeners = new Set<() => void>();

function set(next: string | null) {
  if (next === current) return;
  current = next;
  for (const l of listeners) l();
}

function subscribe(onChange: () => void) {
  listeners.add(onChange);
  return () => {
    listeners.delete(onChange);
  };
}

/** Name this page in the tab title while it is mounted. */
export function usePageTitle(name: string | null | undefined) {
  const clean = name?.trim() || null;
  useEffect(() => {
    if (!clean) return;
    set(clean);
    return () => {
      // Only if it is still ours: the next page's name may already be set.
      if (current === clean) set(null);
    };
  }, [clean]);
}

/** The name the current page gave itself, if any. Read by the shell. */
export function useCurrentPageTitle(): string | null {
  return useSyncExternalStore(subscribe, () => current);
}
