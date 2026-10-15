// Small presentational formatters shared by the shell (sidebar, home
// dispatch view, run spine). Kept free of Relay/DOM imports so they stay
// trivially testable.

const MIN = 60_000;

/**
 * A server timestamp as a Date. Stored times come back as SQLAlchemy wrote
 * them: UTC with no zone ("2026-10-15T05:59:06.462616"), which `new Date`
 * would read as *local* time — hours off, so a minute-old row read "just
 * now" for half a day and dates landed on tomorrow. A stamp without an
 * offset is UTC; one with an offset (a scheduler's next run) is left alone.
 */
export function parseTimestamp(iso: string): Date {
  const naive = /^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?$/.test(iso);
  return new Date(naive ? `${iso.replace(' ', 'T')}Z` : iso);
}
const HOUR = 60 * MIN;
const DAY = 24 * HOUR;

/** "just now" · "4m" · "3h" · "2d" · "12 Mar" — compact, for dense rails. */
export function relativeTime(iso: string): string {
  const then = parseTimestamp(iso).getTime();
  if (!Number.isFinite(then)) return '';
  const delta = Date.now() - then;
  if (delta < MIN) return 'just now';
  if (delta < HOUR) return `${Math.floor(delta / MIN)}m`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h`;
  if (delta < 7 * DAY) return `${Math.floor(delta / DAY)}d`;
  return shortDate(iso);
}

/** "Aug 14", or "Aug 14, 2025" once it is not this year — what a week-old item shows. */
export function shortDate(iso: string): string {
  const d = parseTimestamp(iso);
  if (Number.isNaN(d.getTime())) return '';
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : {year: 'numeric'}),
  });
}

/** "Oct 14, 2026, 9:05 AM" — the exact moment, for a relative time's tooltip. */
export function fullDateTime(iso: string): string {
  const d = parseTimestamp(iso);
  if (Number.isNaN(d.getTime())) return '';
  return d.toLocaleString(undefined, {dateStyle: 'medium', timeStyle: 'short'});
}

export type TimeBucket = 'Today' | 'Yesterday' | 'This week' | 'Earlier';

/**
 * Calendar-day bucketing (not elapsed-time) so a conversation started at
 * 11pm still reads "Today" at 11:30pm and "Yesterday" the next morning.
 */
export function timeBucket(iso: string, now = new Date()): TimeBucket {
  const then = parseTimestamp(iso);
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

/**
 * "Claude Sonnet 4.6 (Anthropic)" → "Sonnet 4.6": a model's label with the
 * family name and provider suffix dropped, for the phone composer's footer.
 */
export function shortModelLabel(label: string): string {
  const short = label
    .replace(/\s*\([^)]*\)\s*$/, '')
    .replace(/^Claude\s+/, '')
    .trim();
  return short || label;
}

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const DAY_ALIASES = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];

/** One day-of-week token, Unix-numbered (0 and 7 are Sunday) or named. */
function cronDay(tok: string): number | null {
  if (/^\d$/.test(tok)) {
    const n = Number(tok);
    return n <= 7 ? n : null;
  }
  const i = DAY_ALIASES.indexOf(tok.toLowerCase());
  return i === -1 ? null : i;
}

/** A day-of-week field as a set of days: `1`, `mon`, `1-5`, `sat,sun`, `1,3-5`. */
function cronDays(field: string): number[] | null {
  const days = new Set<number>();
  for (const part of field.split(',')) {
    const [from, to, ...rest] = part.split('-');
    if (rest.length) return null;
    const a = cronDay(from);
    const b = to === undefined ? a : cronDay(to);
    if (a === null || b === null || b < a) return null;
    for (let d = a; d <= b; d++) days.add(d % 7);
  }
  return [...days].sort((x, y) => x - y);
}

function describeDays(days: number[]): string {
  const key = days.join(',');
  if (key === '1,2,3,4,5') return 'Weekdays';
  if (key === '0,6') return 'Weekends';
  if (days.length === 1) return `${DAY_NAMES[days[0]]}s`;
  // Monday-first, the way a week reads.
  const ordered = [...days.filter((d) => d !== 0), ...days.filter((d) => d === 0)];
  return ordered.map((d) => DAY_NAMES[d].slice(0, 3)).join(', ');
}

function ordinal(n: number): string {
  const tens = n % 100;
  if (tens >= 11 && tens <= 13) return `${n}th`;
  return `${n}${['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'}`;
}

/**
 * A crontab line as a person would say it: "Every 30 minutes", "Weekdays at
 * 7:00", "Mondays at 9:00", "Monthly on the 1st at 8:30". Covers the common
 * shapes only — anything else returns null and the caller shows the raw cron.
 */
export function describeCron(expr: string): string | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [min, hour, dom, month, dow] = parts;
  if (month !== '*') return null;

  const num = (s: string) => (/^\d+$/.test(s) ? Number(s) : null);
  const step = (s: string) => {
    const m = /^\*\/(\d+)$/.exec(s);
    return m ? Number(m[1]) : null;
  };
  const anyDay = dom === '*' && dow === '*';

  if (hour === '*' && anyDay) {
    if (min === '*' || step(min) === 1) return 'Every minute';
    const every = step(min);
    if (every) return `Every ${every} minutes`;
    const at = num(min);
    if (at === null || at > 59) return null;
    return at === 0 ? 'Every hour' : `Hourly at :${String(at).padStart(2, '0')}`;
  }

  const m = num(min);
  if (m === null || m > 59) return null;
  const mm = String(m).padStart(2, '0');

  const everyHours = step(hour);
  if (everyHours && anyDay) {
    const base = everyHours === 1 ? 'Every hour' : `Every ${everyHours} hours`;
    return m === 0 ? base : `${base} at :${mm}`;
  }

  const hours = hour.split(',').map(num);
  if (hours.some((h) => h === null || h > 23)) return null;
  const times = hours.map((h) => `${h}:${mm}`);
  const at = `at ${times.length > 1 ? `${times.slice(0, -1).join(', ')} and ${times[times.length - 1]}` : times[0]}`;

  if (anyDay) return `Daily ${at}`;
  if (dom === '*') {
    const days = cronDays(dow);
    return days ? `${describeDays(days)} ${at}` : null;
  }
  const day = num(dom);
  if (dow === '*' && day !== null && day >= 1 && day <= 31) {
    return `Monthly on the ${ordinal(day)} ${at}`;
  }
  return null;
}

/** Time-of-day greeting for the dispatch view. */
export function greeting(now = new Date()): string {
  const h = now.getHours();
  if (h < 5) return 'Working late';
  if (h < 12) return 'Good morning';
  if (h < 18) return 'Good afternoon';
  return 'Good evening';
}
