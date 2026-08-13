import {useSyncExternalStore} from 'react';

import {fetchRunningTasks} from '../relay/RunningTasksQuery';
import type {RunningTask} from '../lib/types';

/**
 * The running-task list, shared by every component that shows it.
 *
 * This one query does not use the `useLazyLoadQuery` + `usePollingRefresh`
 * pattern the other polled screens use, for two reasons:
 *
 *  1. **Three consumers, one poll.** The root layout, the home page and /tasks
 *     all read it, and on the home page two of them are mounted at once. Three
 *     independent `useLazyLoadQuery` calls would mean three independent pollers
 *     hitting the same endpoint.
 *  2. **It must fail soft.** The root layout renders it outside every route, so
 *     a throw there blanks the whole app — and a QueryBoundary is no help
 *     because the count is woven through the nav. A failed poll keeps the last
 *     good value instead.
 *
 * Polling stops while nothing is running, matching the old react-query
 * `refetchInterval` predicate. Rediscovery is push-based: the paths that start
 * or end a task call `refreshRunningTasks()`.
 */

const POLL_MS = 2000;

let snapshot: RunningTask[] = [];
let loaded = false;
let inFlight: Promise<void> | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let subscriberCount = 0;

const listeners = new Set<() => void>();

function emit() {
  for (const l of listeners) l();
}

function sameTasks(a: RunningTask[], b: RunningTask[]): boolean {
  if (a.length !== b.length) return false;
  return a.every((x, i) => {
    const y = b[i];
    const keys = Object.keys(x) as (keyof RunningTask)[];
    return keys.length === Object.keys(y).length && keys.every((k) => x[k] === y[k]);
  });
}

async function refresh(): Promise<void> {
  // Single-flight: three components mounting at once must not fan out.
  if (inFlight) return inFlight;
  inFlight = (async () => {
    let changed = false;
    try {
      const next = await fetchRunningTasks();
      // useSyncExternalStore requires a referentially stable snapshot — swapping
      // in an equal-but-new array every 2s would re-render the whole shell.
      if (!sameTasks(snapshot, next)) {
        snapshot = next;
        changed = true;
      }
    } catch {
      // Keep the last good value; a blip should not empty the nav badge.
    } finally {
      inFlight = null;
      // A first attempt that returned an empty list leaves the snapshot
      // untouched, so `loaded` is what moves /tasks off its loading state.
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
  if (snapshot.length === 0) return; // idle — see the push-based note above
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
  // Fresh read on mount; concurrent mounts collapse into one request.
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

export function useRunningTasks(): RunningTask[] {
  return useSyncExternalStore(subscribe, () => snapshot);
}

/** False until the first read settles, so callers can tell empty from pending. */
export function useRunningTasksLoaded(): boolean {
  return useSyncExternalStore(subscribe, () => loaded);
}

/**
 * Force a re-read — and restart polling if this turned up a new task. Call from
 * anywhere a task is known to have started or ended.
 */
export function refreshRunningTasks(): Promise<void> {
  return refresh().then(schedule);
}
