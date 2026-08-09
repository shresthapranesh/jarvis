// Small presentational formatters shared by the shell (sidebar, home
// dispatch view, run spine). Kept free of Relay/DOM imports so they stay
// trivially testable.

const MIN = 60_000;
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/** "just now" · "4m" · "3h" · "2d" · "12 Mar" — compact, for dense rails. */
export function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  if (!Number.isFinite(then)) return '';
  const delta = Date.now() - then;
  if (delta < MIN) return 'just now';
  if (delta < HOUR) return `${Math.floor(delta / MIN)}m`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h`;
  if (delta < 7 * DAY) return `${Math.floor(delta / DAY)}d`;
  return new Date(then).toLocaleDateString(undefined, {day: 'numeric', month: 'short'});
}

export type TimeBucket = 'Today' | 'Yesterday' | 'This week' | 'Earlier';

/**
 * Calendar-day bucketing (not elapsed-time) so a conversation started at
 * 11pm still reads "Today" at 11:30pm and "Yesterday" the next morning.
 */
export function timeBucket(iso: string, now = new Date()): TimeBucket {
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return 'Earlier';

  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = then.getTime();

  if (t >= startOfToday) return 'Today';
  if (t >= startOfToday - DAY) return 'Yesterday';
  if (t >= startOfToday - 7 * DAY) return 'This week';
  return 'Earlier';
}

export const BUCKET_ORDER: TimeBucket[] = ['Today', 'Yesterday', 'This week', 'Earlier'];

/** 1_240 → "1.2k". Used for token counts on the spine. */
export function compactNumber(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

/** Time-of-day greeting for the dispatch view. */
export function greeting(now = new Date()): string {
  const h = now.getHours();
  if (h < 5) return 'Working late';
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}
