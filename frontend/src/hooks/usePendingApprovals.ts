import {useSyncExternalStore} from 'react';

import type {PendingApproval} from '../lib/types';
import {fetchPendingApprovals} from '../relay/PendingApprovalsQuery';

/**
 * The pending-approval inbox, shared by the nav badge and the /approvals page.
 *
 * Built on the same external-store shape as `useRunningTasks`, for the same
 * two reasons: several components read it at once (so one poll, not N), and
 * the root layout renders the badge outside every route (so a failed poll must
 * keep the last good value rather than throw and blank the shell).
 *
 * It differs in one way that matters: `useRunningTasks` stops polling once the
 * list is empty, because a task can only appear via a path that also calls
 * `refreshRunningTasks`. An approval has no such chokepoint — a board task can
 * block on a question with no run in flight anywhere, and a workflow can pause
 * long after the user navigated away — so this polls whenever it is subscribed
 * and the tab is visible. The interval is correspondingly slower: an inbox is
 * read at human pace, not at token pace.
 */

const POLL_MS = 5000;

let snapshot: PendingApproval[] = [];
let loaded = false;
let inFlight: Promise<void> | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let subscriberCount = 0;

const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function same(a: PendingApproval[], b: PendingApproval[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((x, i) => {
    const y = b[i];
    const keys = Object.keys(x) as (keyof PendingApproval)[];
    return keys.length === Object.keys(y).length && keys.every((k) => x[k] === y[k]);
  });
}

async function refresh(force = false): Promise<void> {
  if (inFlight) {
    // Single-flight: concurrent mounts must not fan out into N requests.
    if (!force) return inFlight;
    // A forced read follows a write. A poll already in flight was sent before
    // that write, so its response cannot contain it — adopting that snapshot
    // would leave the resolved item on screen until the next tick, and there
    // may not be one (polling is paused while the tab is hidden). Wait the
    // stale request out, then issue a fresh one.
    await inFlight;
  }
  inFlight = (async () => {
    let changed = false;
    try {
      const next = await fetchPendingApprovals();
      // useSyncExternalStore needs a referentially stable snapshot — swapping
      // in an equal-but-new array every poll would re-render the whole shell.
      if (!same(snapshot, next)) {
        snapshot = next;
        changed = true;
      }
    } catch {
      // Keep the last good value; a blip should not empty the nav badge.
    } finally {
      inFlight = null;
      if (!loaded) {
        loaded = true;
        changed = true;
      }
      if (changed) emit();
    }
  })();
  return inFlight;
}

function schedule() {
  if (timer !== null || subscriberCount === 0) return;
  if (document.hidden) return; // resumed by the visibility listener
  timer = setTimeout(() => {
    timer = null;
    void refresh().then(schedule);
  }, POLL_MS);
}

function clearTimer() {
  if (timer !== null) {
    clearTimeout(timer);
    timer = null;
  }
}

function onVisibilityChange() {
  if (document.hidden) {
    clearTimer();
    return;
  }
  void refresh().then(schedule);
}

function subscribe(onStoreChange: () => void) {
  listeners.add(onStoreChange);
  subscriberCount += 1;
  if (subscriberCount === 1) {
    document.addEventListener('visibilitychange', onVisibilityChange);
  }
  void refresh().then(schedule);

  return () => {
    listeners.delete(onStoreChange);
    subscriberCount -= 1;
    if (subscriberCount === 0) {
      clearTimer();
      document.removeEventListener('visibilitychange', onVisibilityChange);
    }
  };
}

export function usePendingApprovals(): PendingApproval[] {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** False until the first read settles, so callers can tell empty from pending. */
export function usePendingApprovalsLoaded(): boolean {
  return useSyncExternalStore(subscribe, () => loaded);
}

/** Force a re-read — call right after resolving one, so it leaves the list. */
export function refreshPendingApprovals(): Promise<void> {
  return refresh(true).then(schedule);
}
