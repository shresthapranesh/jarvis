import * as stylex from '@stylexjs/stylex';
import {useState} from 'react';

import {describeCron} from '../lib/format';
import {colors, type} from '../theme/tokens.stylex';
import {CalendarIcon} from './icons';
import {field} from './ui';

/* ── The schedule, picked rather than typed ────────────────────────────
   The stored value is still a five-field cron line (the server's
   `cron.rs`, Unix day-of-week), so this is a builder over it: the common
   shapes get controls, and anything they can't express round-trips
   through the Cron tab untouched. A cron that parses into a shape opens on
   that shape; one that doesn't opens on Cron, as written. */

type Mode = 'manual' | 'interval' | 'daily' | 'weekly' | 'monthly' | 'custom';

interface Builder {
  mode: Mode;
  every: number;
  unit: 'minutes' | 'hours';
  /** "HH:MM", the `<input type="time">` spelling. */
  time: string;
  /** Cron day-of-week numbers, 0 = Sunday. */
  days: number[];
  dayOfMonth: number;
  custom: string;
}

const MODES: {id: Mode; label: string}[] = [
  {id: 'manual', label: 'Manual'},
  {id: 'interval', label: 'Interval'},
  {id: 'daily', label: 'Daily'},
  {id: 'weekly', label: 'Weekly'},
  {id: 'monthly', label: 'Monthly'},
  {id: 'custom', label: 'Cron'},
];

/** Monday first, the way a week reads; the values are cron's. */
const WEEK: {dow: number; short: string; name: string}[] = [
  {dow: 1, short: 'M', name: 'Monday'},
  {dow: 2, short: 'T', name: 'Tuesday'},
  {dow: 3, short: 'W', name: 'Wednesday'},
  {dow: 4, short: 'T', name: 'Thursday'},
  {dow: 5, short: 'F', name: 'Friday'},
  {dow: 6, short: 'S', name: 'Saturday'},
  {dow: 0, short: 'S', name: 'Sunday'},
];

const DEFAULTS: Omit<Builder, 'mode' | 'custom'> = {
  every: 15,
  unit: 'minutes',
  time: '09:00',
  days: [1, 2, 3, 4, 5],
  dayOfMonth: 1,
};

const pad = (n: number) => String(n).padStart(2, '0');

function parseDays(dow: string): number[] | null {
  const out = new Set<number>();
  for (const part of dow.split(',')) {
    const range = /^(\d)-(\d)$/.exec(part);
    if (range) {
      const [a, b] = [Number(range[1]), Number(range[2])];
      if (a > b || b > 7) return null;
      for (let d = a; d <= b; d++) out.add(d % 7);
    } else if (/^\d$/.test(part) && Number(part) <= 7) {
      out.add(Number(part) % 7);
    } else {
      return null;
    }
  }
  return out.size ? [...out].sort((x, y) => x - y) : null;
}

/** 1,2,3,4,5 → "1-5": runs of three or more become ranges. */
function compressDays(days: number[]): string {
  const sorted = [...days].sort((a, b) => a - b);
  const parts: string[] = [];
  for (let i = 0; i < sorted.length; ) {
    let j = i;
    while (j + 1 < sorted.length && sorted[j + 1] === sorted[j] + 1) j++;
    if (j - i >= 2) parts.push(`${sorted[i]}-${sorted[j]}`);
    else for (let k = i; k <= j; k++) parts.push(String(sorted[k]));
    i = j + 1;
  }
  return parts.join(',');
}

function parseSchedule(expr: string): Builder {
  const s = expr.trim();
  const base = {...DEFAULTS, custom: s};
  if (!s) return {...base, mode: 'manual'};
  const custom: Builder = {...base, mode: 'custom'};

  const parts = s.split(/\s+/);
  if (parts.length !== 5) return custom;
  const [min, hour, dom, month, dow] = parts;
  if (month !== '*') return custom;
  const anyDay = dom === '*' && dow === '*';
  const num = (x: string, max: number, lo = 0) =>
    /^\d+$/.test(x) && Number(x) >= lo && Number(x) <= max ? Number(x) : null;
  const step = (x: string) => {
    const m = /^\*\/(\d+)$/.exec(x);
    return m ? Number(m[1]) : null;
  };

  if (anyDay && hour === '*') {
    if (min === '*') return {...base, mode: 'interval', every: 1, unit: 'minutes'};
    const every = step(min);
    if (every && every <= 59) return {...base, mode: 'interval', every, unit: 'minutes'};
    if (min === '0') return {...base, mode: 'interval', every: 1, unit: 'hours'};
    return custom;
  }

  const m = num(min, 59);
  if (m === null) return custom;
  const everyHours = step(hour);
  if (anyDay && everyHours && everyHours <= 23 && m === 0) {
    return {...base, mode: 'interval', every: everyHours, unit: 'hours'};
  }
  const h = num(hour, 23);
  if (h === null) return custom;
  const time = `${pad(h)}:${pad(m)}`;

  if (anyDay) return {...base, mode: 'daily', time};
  if (dom === '*') {
    const days = parseDays(dow);
    return days ? {...base, mode: 'weekly', time, days} : custom;
  }
  const day = num(dom, 31, 1);
  if (day !== null && dow === '*') return {...base, mode: 'monthly', time, dayOfMonth: day};
  return custom;
}

function buildSchedule(b: Builder): string {
  const [h, m] = b.time.split(':').map(Number);
  const at = `${m || 0} ${h || 0}`;
  switch (b.mode) {
    case 'manual':
      return '';
    case 'interval':
      if (b.unit === 'minutes') return b.every <= 1 ? '* * * * *' : `*/${b.every} * * * *`;
      return b.every <= 1 ? '0 * * * *' : `0 */${b.every} * * *`;
    case 'daily':
      return `${at} * * *`;
    case 'weekly':
      return b.days.length === 7 ? `${at} * * *` : `${at} * * ${compressDays(b.days)}`;
    case 'monthly':
      return `${at} ${b.dayOfMonth} * *`;
    case 'custom':
      return b.custom.trim();
  }
}

const clampInt = (raw: string, lo: number, hi: number) =>
  Math.min(hi, Math.max(lo, Math.round(Number(raw) || lo)));

export function ScheduleEditor({
  value,
  onChange,
  disabled,
}: {
  value: string;
  onChange: (cron: string) => void;
  disabled?: boolean;
}) {
  // Local, because the builder holds more than the cron says: the days you
  // picked before switching to Daily are still there when you switch back.
  const [b, setB] = useState<Builder>(() => parseSchedule(value));

  function update(patch: Partial<Builder>) {
    const next = {...b, ...patch};
    // Opening Cron starts from what the builder had, not a blank line.
    if (patch.mode === 'custom' && b.mode !== 'custom') next.custom = buildSchedule(b);
    setB(next);
    onChange(buildSchedule(next));
  }

  function toggleDay(dow: number) {
    const has = b.days.includes(dow);
    // A weekly schedule with no days would never fire.
    if (has && b.days.length === 1) return;
    update({days: has ? b.days.filter((d) => d !== dow) : [...b.days, dow]});
  }

  const cron = buildSchedule(b);
  const readable = cron ? describeCron(cron) : null;

  const timeInput = (
    <input
      type="time"
      {...stylex.props(field.input, s.time)}
      value={b.time}
      onChange={(e) => e.target.value && update({time: e.target.value})}
      disabled={disabled}
      aria-label="Time of day"
    />
  );

  return (
    <div {...stylex.props(s.root)}>
      <div {...stylex.props(s.modes)} role="radiogroup" aria-label="Schedule">
        {MODES.map((mode) => (
          <button
            key={mode.id}
            type="button"
            role="radio"
            aria-checked={b.mode === mode.id}
            {...stylex.props(s.mode, b.mode === mode.id && s.modeOn)}
            onClick={() => update({mode: mode.id})}
            disabled={disabled}
          >
            {mode.label}
          </button>
        ))}
      </div>

      {b.mode === 'manual' && (
        <p {...stylex.props(s.note)}>
          No schedule — it runs when you press Run now, or when a workflow or the agent starts it.
        </p>
      )}

      {b.mode === 'interval' && (
        <div {...stylex.props(s.line)}>
          <span {...stylex.props(s.word)}>Every</span>
          <input
            type="number"
            {...stylex.props(field.input, s.number)}
            min={1}
            max={b.unit === 'minutes' ? 59 : 23}
            value={b.every}
            onChange={(e) =>
              update({every: clampInt(e.target.value, 1, b.unit === 'minutes' ? 59 : 23)})
            }
            disabled={disabled}
            aria-label="Interval"
          />
          <select
            {...stylex.props(field.select, field.selectChrome, s.grow)}
            value={b.unit}
            onChange={(e) => {
              const unit = e.target.value as Builder['unit'];
              update({unit, every: Math.min(b.every, unit === 'minutes' ? 59 : 23)});
            }}
            disabled={disabled}
            aria-label="Interval unit"
          >
            <option value="minutes">{b.every === 1 ? 'minute' : 'minutes'}</option>
            <option value="hours">{b.every === 1 ? 'hour' : 'hours'}</option>
          </select>
        </div>
      )}

      {b.mode === 'daily' && (
        <div {...stylex.props(s.line)}>
          <span {...stylex.props(s.word)}>At</span>
          {timeInput}
        </div>
      )}

      {b.mode === 'weekly' && (
        <>
          <div {...stylex.props(s.week)} role="group" aria-label="Days of the week">
            {WEEK.map((d) => {
              const on = b.days.includes(d.dow);
              return (
                <button
                  key={d.dow}
                  type="button"
                  aria-pressed={on}
                  title={d.name}
                  aria-label={d.name}
                  {...stylex.props(s.day, on && s.dayOn)}
                  onClick={() => toggleDay(d.dow)}
                  disabled={disabled}
                >
                  {d.short}
                </button>
              );
            })}
          </div>
          <div {...stylex.props(s.line)}>
            <span {...stylex.props(s.word)}>At</span>
            {timeInput}
          </div>
        </>
      )}

      {b.mode === 'monthly' && (
        <>
          <div {...stylex.props(s.line)}>
            <span {...stylex.props(s.word)}>On day</span>
            <input
              type="number"
              {...stylex.props(field.input, s.number)}
              min={1}
              max={31}
              value={b.dayOfMonth}
              onChange={(e) => update({dayOfMonth: clampInt(e.target.value, 1, 31)})}
              disabled={disabled}
              aria-label="Day of the month"
            />
            <span {...stylex.props(s.word)}>at</span>
            {timeInput}
          </div>
          {b.dayOfMonth > 28 && (
            <p {...stylex.props(s.note)}>Months without a day {b.dayOfMonth} are skipped.</p>
          )}
        </>
      )}

      {b.mode === 'custom' && (
        <div {...stylex.props(field.group)}>
          <input
            {...stylex.props(field.input, s.cronInput)}
            value={b.custom}
            onChange={(e) => update({custom: e.target.value})}
            placeholder="0 9 * * 1-5"
            disabled={disabled}
            spellCheck={false}
            aria-label="Cron expression"
          />
          <span {...stylex.props(s.note)}>minute · hour · day of month · month · day of week</span>
        </div>
      )}

      {cron && (
        <div {...stylex.props(s.summary)}>
          <CalendarIcon size={12} />
          <span {...stylex.props(s.summaryText)}>{readable ?? 'Custom schedule'}</span>
          <code {...stylex.props(s.summaryCron)}>{cron}</code>
        </div>
      )}
      {cron && <p {...stylex.props(s.note)}>Times are in the server's local time zone.</p>}
    </div>
  );
}

const s = stylex.create({
  root: {display: 'flex', flexDirection: 'column', gap: 10},
  modes: {
    display: 'grid',
    gridTemplateColumns: 'repeat(3, minmax(0, 1fr))',
    gap: 2,
    padding: 2,
    borderRadius: 3,
    backgroundColor: colors.surface,
  },
  mode: {
    fontFamily: 'inherit',
    fontSize: type.tSmall,
    fontWeight: 500,
    paddingBlock: 6,
    borderWidth: 0,
    borderRadius: 2,
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    transition: 'background 0.12s, color 0.12s',
  },
  modeOn: {
    backgroundColor: {default: colors.surface3, ':hover': colors.surface3},
    color: {default: colors.text, ':hover': colors.text},
  },
  line: {display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap'},
  word: {fontSize: type.tUi, color: colors.textDim},
  number: {width: 64, fontVariantNumeric: 'tabular-nums'},
  time: {fontVariantNumeric: 'tabular-nums', minWidth: 110},
  grow: {flex: 1, minWidth: 100},
  week: {display: 'grid', gridTemplateColumns: 'repeat(7, minmax(0, 1fr))', gap: 4},
  day: {
    fontFamily: 'inherit',
    fontSize: type.tSmall,
    fontWeight: 600,
    aspectRatio: '1',
    maxHeight: 36,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: 'transparent',
    borderRadius: 2,
    backgroundColor: {default: colors.surface2, ':hover': colors.surface3},
    color: colors.textDim,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    transition: 'background 0.12s, color 0.12s',
  },
  dayOn: {
    backgroundColor: {default: colors.accent, ':hover': colors.accent},
    color: colors.accentContrast,
  },
  cronInput: {fontFamily: type.mono, fontSize: type.tUi},
  note: {margin: 0, fontSize: type.tSmall, color: colors.textDim, lineHeight: 1.45},
  summary: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    paddingBlock: 8,
    paddingInline: 10,
    borderRadius: 2,
    backgroundColor: colors.surface,
    color: colors.textDim,
    minWidth: 0,
  },
  summaryText: {fontSize: type.tUi, color: colors.text, flex: 1, minWidth: 0},
  summaryCron: {
    fontFamily: type.mono,
    fontSize: type.tSmall,
    color: colors.textDim,
    whiteSpace: 'nowrap',
  },
});
