import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, layout, type} from '../theme/tokens.stylex';

/* ── Styles for routes/automation.tsx ──────────────────────────────────
   The command-center list: a header with KPI chips and filters, then one
   card per automation. The card is a five-column grid — status rail, type
   icon, name/badges, run times, actions — and most of the variants below
   are the four input types and the five run statuses it renders. */

export const kpi = stylex.create({
  grid: {
    display: 'grid',
    gridTemplateColumns: {
      default: 'repeat(4, minmax(0, 1fr))',
      '@media (max-width: 600px)': 'repeat(2, minmax(0, 1fr))',
    },
    gap: 10,
    maxWidth: 720,
  },
  chip: {
    backgroundColor: colors.surface,
    borderRadius: 3,
    paddingBlock: 10,
    paddingInline: 14,
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
  },
  value: {
    fontSize: type.tPage,
    fontWeight: 600,
    color: colors.text,
    letterSpacing: '-0.02em',
    display: 'flex',
    alignItems: 'baseline',
    gap: 4,
  },
  suffix: {fontSize: type.tBody, fontWeight: 400, color: colors.textDim},
  label: {
    fontSize: type.tMicro,
    color: colors.textDim,
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
  },
});

export const filters = stylex.create({
  bar: {display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap'},

  search: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: 'transparent', ':focus-within': colors.borderStrong},
    borderRadius: 3,
    paddingBlock: 6,
    paddingInline: 10,
    color: {default: colors.textDim, ':focus-within': colors.text},
    flex: 1,
    maxWidth: 320,
    transition: 'border-color 0.15s',
  },
  searchInput: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    outline: 'none',
    flex: 1,
    fontSize: type.tUi,
    color: colors.text,
    fontFamily: 'inherit',
    minWidth: 0,
    '::placeholder': {color: colors.textDim},
  },
  searchClear: {
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    borderStyle: 'none',
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
    padding: 2,
    display: 'flex',
    borderRadius: 2,
  },

  pills: {
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    backgroundColor: colors.surface,
    borderRadius: 3,
    padding: 3,
  },
  pill: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    color: {default: colors.textDim, ':hover': colors.text},
    fontSize: type.tSmall,
    paddingBlock: 4,
    paddingInline: 10,
    borderRadius: 2,
    cursor: 'pointer',
    fontFamily: 'inherit',
    textTransform: 'capitalize',
    transition: 'background 0.15s, color 0.15s',
  },
  pillOn: {backgroundColor: colors.accentDim, color: colors.accent},

  groupBy: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    backgroundColor: colors.surface,
    borderRadius: 3,
    paddingBlock: 6,
    paddingInline: 10,
    color: colors.textDim,
    fontSize: type.tSmall,
    position: 'relative',
  },
  groupByLabel: {textTransform: 'uppercase', letterSpacing: '0.06em', fontSize: type.tMicro},
  groupBySelect: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    outline: 'none',
    color: colors.text,
    fontSize: type.tUi,
    fontFamily: 'inherit',
    appearance: 'none',
    cursor: 'pointer',
    paddingInlineEnd: 2,
    // The popup is painted by the OS, which does not inherit the transparent
    // background above — it needs a real colour or it renders unreadable.
    '::picker(select)': {backgroundColor: colors.bg, color: colors.text},
  },
});

export const list = stylex.create({
  container: {
    flex: 1,
    overflowY: 'auto',
    paddingBlock: {default: '18px 60px', '@media (max-width: 860px)': '12px 60px'},
    paddingInline: {default: layout.pageGutter, '@media (max-width: 768px)': 16},
    display: 'flex',
    flexDirection: 'column',
    gap: 22,
  },
  emptyMsg: {
    color: colors.textDim,
    fontSize: type.tBody,
    paddingBlock: 18,
    paddingInline: 4,
    textAlign: 'center',
  },
  group: {display: 'flex', flexDirection: 'column', gap: 8},
  groupLabel: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    fontSize: type.tNano,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.09em',
    color: colors.textDim,
    paddingBlock: '0 2px',
    paddingInline: 2,
  },
  groupCount: {
    backgroundColor: colors.surface,
    borderRadius: 2,
    paddingBlock: 1,
    paddingInline: 7,
    fontSize: type.tNano,
    letterSpacing: '0.04em',
    color: colors.textDim,
  },
  cards: {display: 'flex', flexDirection: 'column', gap: 10},
});

/** One automation. The grid columns are: rail, icon, main, meta, actions. */
export const card = stylex.create({
  root: {
    position: 'relative',
    display: 'grid',
    gridTemplateColumns: {
      default: '4px 36px minmax(0, 1fr) auto auto',
      // The meta column is the first thing worth dropping on a narrow screen,
      // then the type icon — the type chip under the name still says it.
      '@media (max-width: 600px)': '4px minmax(0, 1fr) auto',
    },
    alignItems: 'center',
    gap: {default: 14, '@media (max-width: 600px)': 10},
    paddingBlock: 14,
    paddingInlineStart: 0,
    paddingInlineEnd: 16,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderRadius: 3,
    cursor: 'pointer',
    overflow: 'hidden',
    outline: {default: null, ':focus-visible': `2px solid ${colors.accent}`},
    outlineOffset: 2,
    transition: 'background 0.15s',
    // The action row rests dimmed and comes up with the card; a child cannot
    // see its parent's :hover, so the card publishes it.
    '--auto-actions-opacity': {default: '0.55', ':hover': '1', ':focus-within': '1'},
  },
  off: {opacity: 0.65},
  /** The card's type icon, which gives up its column on a phone. */
  icon: {display: {default: null, '@media (max-width: 600px)': 'none'}},

  main: {display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0},
  titlebar: {display: 'flex', alignItems: 'center', gap: 8, minWidth: 0},
  name: {
    fontSize: type.tBody,
    fontWeight: 600,
    color: colors.text,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  pausedPill: {
    fontSize: type.tNano,
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
    backgroundColor: colors.surface2,
    paddingBlock: 2,
    paddingInline: 7,
    borderRadius: 2,
    flexShrink: 0,
  },
  desc: {
    fontSize: type.tSmall,
    color: colors.textDim,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  badges: {display: 'flex', alignItems: 'center', gap: 6, marginBlockStart: 2, flexWrap: 'wrap'},

  metaCol: {
    display: {default: 'flex', '@media (max-width: 600px)': 'none'},
    flexDirection: 'column',
    gap: 3,
    alignItems: 'flex-end',
    minWidth: 0,
    textAlign: 'right',
  },
  metaLine: {
    display: 'flex',
    alignItems: 'center',
    gap: 5,
    fontSize: type.tSmall,
    color: colors.textDim,
    whiteSpace: 'nowrap',
  },
  metaNext: {color: colors.accent},
  metaNever: {fontStyle: 'italic'},
  metaStats: {fontSize: type.tMicro, letterSpacing: '0.04em'},

  actions: {
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    // Recessive-until-hover resolves to permanently recessive on touch.
    opacity: {default: 'var(--auto-actions-opacity)', '@media (hover: none)': 1},
    transition: 'opacity 0.15s',
    flexShrink: 0,
  },
  action: {
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: 'transparent',
    color: {default: colors.textDim, ':hover': colors.text},
    borderRadius: 2,
    padding: 6,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    cursor: 'pointer',
    transition: 'background 0.12s, color 0.12s, border-color 0.12s',
  },
  actionPlay: {
    backgroundColor: {default: colors.surface, ':hover': `rgba(${channels.accent}, 0.15)`},
    color: {default: colors.textDim, ':hover': colors.accent},
  },
  actionDanger: {
    backgroundColor: {default: colors.surface, ':hover': `rgba(${channels.danger}, 0.12)`},
    color: {default: colors.textDim, ':hover': colors.danger},
  },

  toggle: {
    display: 'inline-flex',
    alignItems: 'center',
    width: 26,
    height: 14,
    borderRadius: 2,
    padding: 2,
    transition: 'background 0.15s',
  },
  toggleOn: {backgroundColor: colors.accent, justifyContent: 'flex-end'},
  toggleOff: {backgroundColor: colors.border, justifyContent: 'flex-start'},
  toggleDot: {
    width: 10,
    height: 10,
    borderRadius: '50%',
    backgroundColor: '#fff',
    display: 'block',
  },
});

/** The 4px status rail down the card's leading edge. */
export const rail = stylex.create({
  base: {width: 4, height: '100%', backgroundColor: colors.border, alignSelf: 'stretch'},
  ok: {backgroundColor: colors.ok, boxShadow: `0 0 12px rgba(${channels.ok}, 0.35)`},
  err: {backgroundColor: colors.danger, boxShadow: `0 0 12px rgba(${channels.danger}, 0.35)`},
  run: {
    backgroundImage: `linear-gradient(180deg, ${colors.accentStrong}, ${colors.accent})`,
    boxShadow: `0 0 14px rgba(${channels.accent}, 0.5)`,
    animationName: kf.railPulse,
    animationDuration: '1.6s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  idle: {backgroundColor: colors.textDim, opacity: 0.45},
  off: {backgroundColor: colors.border},
});

/** The rounded square holding the input-type glyph. */
export const typeIcon = stylex.create({
  base: {
    width: 32,
    height: 32,
    borderRadius: 3,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
  },
  prompt: {
    color: colors.accent,
    backgroundColor: colors.accentDim,
  },
  code: {
    color: colors.ok,
    backgroundColor: `rgba(${channels.ok}, 0.1)`,
  },
  webhook: {
    color: colors.webhookText,
    backgroundColor: colors.webhookBg,
  },
  monitor: {
    color: colors.warn,
    backgroundColor: `rgba(${channels.warn}, 0.1)`,
  },
});

/** Input-type and schedule chips under the name. */
export const chip = stylex.create({
  base: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    fontSize: type.tNano,
    fontWeight: 500,
    paddingBlock: 2,
    paddingInline: 7,
    borderRadius: 2,
    letterSpacing: '0.02em',
    textTransform: 'capitalize',
  },
  prompt: {backgroundColor: colors.accentDim, color: colors.accent},
  code: {backgroundColor: `rgba(${channels.ok}, 0.12)`, color: colors.ok},
  webhook: {backgroundColor: colors.webhookBg, color: colors.webhookText},
  monitor: {backgroundColor: `rgba(${channels.warn}, 0.12)`, color: colors.warn},
  schedule: {
    backgroundColor: colors.surface2,
    color: colors.textDim,
    fontFamily: type.mono,
    textTransform: 'none',
  },
  /** Once the cron is put into words it is prose, not a code literal. */
  scheduleReadable: {fontFamily: type.body},
  adhoc: {
    backgroundColor: colors.surface2,
    color: colors.textDim,
  },
});

/** Run-status dot, shared with the runs panel. */
export const statusDot = stylex.create({
  base: {
    width: 7,
    height: 7,
    borderRadius: '50%',
    flexShrink: 0,
    backgroundColor: colors.textDim,
  },
  done: {backgroundColor: colors.ok, boxShadow: `0 0 6px rgba(${channels.ok}, 0.45)`},
  error: {backgroundColor: colors.danger, boxShadow: `0 0 6px rgba(${channels.danger}, 0.45)`},
  running: {
    backgroundColor: colors.accent,
    boxShadow: `0 0 6px rgba(${channels.accent}, 0.55)`,
    animationName: kf.railPulse,
    animationDuration: '1.6s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  no_change: {
    backgroundColor: 'transparent',
    borderWidth: 1.5,
    borderStyle: 'solid',
    borderColor: colors.ok,
  },
  skipped: {backgroundColor: colors.textDim},
  stopped: {backgroundColor: colors.textDim},
  blocked: {backgroundColor: colors.warn, boxShadow: `0 0 6px rgba(${channels.warn}, 0.45)`},
});

export const empty = stylex.create({
  root: {
    marginBlock: '60px 0',
    marginInline: 'auto',
    maxWidth: 420,
    textAlign: 'center',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: 14,
    padding: 32,
  },
  glow: {
    width: 64,
    height: 64,
    borderRadius: '50%',
    backgroundImage: `radial-gradient(circle, rgba(${channels.accent}, 0.3) 0%, rgba(${channels.accent}, 0.05) 60%, transparent 100%)`,
    color: colors.accent,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    marginBlockEnd: 4,
  },
  title: {margin: 0, fontSize: type.tTitle, fontWeight: 600, color: colors.text},
  body: {margin: 0, color: colors.textDim, fontSize: type.tUi, lineHeight: 1.5},
});

/** The slide-in editor sheet. */
/** A red note under a destructive confirm dialog's body. */
export const confirmWarn = stylex.create({
  base: {color: colors.danger, fontSize: type.tUi},
});
