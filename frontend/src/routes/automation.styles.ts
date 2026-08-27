import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, css, layout, type} from '../theme/tokens.stylex';

/* ── Styles for routes/automation.tsx ──────────────────────────────────
   The command-center list: a header with KPI chips and filters, then one
   card per automation. The card is a five-column grid — status rail, type
   icon, name/badges, run times, actions — and most of the variants below
   are the four input types and the five run statuses it renders. */

/** Page header: title row, KPI chips, and the filter bar. */
export const header = stylex.create({
  root: {
    paddingBlock: {default: '22px 14px', '@media (max-width: 860px)': '16px 12px'},
    paddingInline: {default: 28, '@media (max-width: 860px)': 16},
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 14,
  },
  titleRow: {
    display: 'flex',
    alignItems: 'flex-end',
    justifyContent: 'space-between',
    gap: 16,
    flexWrap: 'wrap',
  },
  titleBlock: {display: 'flex', flexDirection: 'column', gap: 3},
  title: {
    margin: 0,
    fontSize: '1.35rem',
    fontWeight: 600,
    letterSpacing: '-0.01em',
    color: colors.text,
  },
  subtitle: {fontSize: '0.75rem', color: colors.textDim},
});

/** The primary "New automation" button, used in the header and the empty state. */
export const newBtn = stylex.create({
  base: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    backgroundImage: `linear-gradient(135deg, ${colors.accentStrong}, ${colors.accent})`,
    borderStyle: 'none',
    borderRadius: 8,
    color: colors.accentContrast,
    fontSize: '0.8rem',
    fontFamily: 'inherit',
    fontWeight: 500,
    paddingBlock: 8,
    paddingInline: 14,
    cursor: 'pointer',
    boxShadow: {
      default: `0 2px 10px rgba(${channels.accent}, 0.3)`,
      ':hover': `0 4px 18px rgba(${channels.accent}, 0.5)`,
    },
    transform: {default: null, ':hover': 'translateY(-1px)'},
    transition: 'box-shadow 0.2s, transform 0.15s',
  },
});

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
    backgroundImage: `linear-gradient(180deg, rgba(${channels.tint}, 0.03), rgba(${channels.tint}, 0.01))`,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 10,
    paddingBlock: 10,
    paddingInline: 14,
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
  },
  value: {
    fontSize: '1.35rem',
    fontWeight: 600,
    color: colors.text,
    letterSpacing: '-0.02em',
    display: 'flex',
    alignItems: 'baseline',
    gap: 4,
  },
  suffix: {fontSize: '0.85rem', fontWeight: 400, color: colors.textDim},
  label: {
    fontSize: '0.66rem',
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
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus-within': colors.accent},
    borderRadius: 8,
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
    fontSize: '0.82rem',
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
    borderRadius: 4,
  },

  pills: {
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 8,
    padding: 3,
  },
  pill: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    color: {default: colors.textDim, ':hover': colors.text},
    fontSize: '0.74rem',
    paddingBlock: 4,
    paddingInline: 10,
    borderRadius: 6,
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
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 8,
    paddingBlock: 6,
    paddingInline: 10,
    color: colors.textDim,
    fontSize: '0.74rem',
    position: 'relative',
  },
  groupByLabel: {textTransform: 'uppercase', letterSpacing: '0.06em', fontSize: '0.66rem'},
  groupBySelect: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    outline: 'none',
    color: colors.text,
    fontSize: '0.78rem',
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
    paddingInline: {default: 28, '@media (max-width: 860px)': 16},
    display: 'flex',
    flexDirection: 'column',
    gap: 22,
  },
  emptyMsg: {
    color: colors.textDim,
    fontSize: '0.85rem',
    paddingBlock: 18,
    paddingInline: 4,
    textAlign: 'center',
  },
  group: {display: 'flex', flexDirection: 'column', gap: 8},
  groupLabel: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    fontSize: '0.65rem',
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.09em',
    color: colors.textDim,
    paddingBlock: '0 2px',
    paddingInline: 2,
  },
  groupCount: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 999,
    paddingBlock: 1,
    paddingInline: 7,
    fontSize: '0.62rem',
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
      // The meta column is the first thing worth dropping on a narrow screen.
      '@media (max-width: 600px)': '4px 36px minmax(0, 1fr) auto',
    },
    alignItems: 'center',
    gap: 14,
    paddingBlock: 14,
    paddingInlineStart: 0,
    paddingInlineEnd: 16,
    backgroundImage: {
      default: `linear-gradient(180deg, rgba(${channels.tint}, 0.035), rgba(${channels.tint}, 0.015))`,
      ':hover': `linear-gradient(180deg, rgba(${channels.accent}, 0.05), rgba(${channels.tint}, 0.02))`,
    },
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': `rgba(${channels.accent}, 0.35)`},
    borderRadius: 12,
    cursor: 'pointer',
    overflow: 'hidden',
    transform: {default: null, ':hover': 'translateY(-1px)'},
    boxShadow: {default: null, ':hover': `0 6px 22px rgba(${channels.shadow}, 0.35)`},
    outline: {default: null, ':focus-visible': `2px solid ${colors.accent}`},
    outlineOffset: 2,
    transition: 'border-color 0.15s, transform 0.12s, box-shadow 0.18s, background 0.15s',
    // The action row rests dimmed and comes up with the card; a child cannot
    // see its parent's :hover, so the card publishes it.
    '--auto-actions-opacity': {default: '0.55', ':hover': '1', ':focus-within': '1'},
  },
  off: {opacity: 0.65},

  main: {display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0},
  titlebar: {display: 'flex', alignItems: 'center', gap: 8, minWidth: 0},
  name: {
    fontSize: '0.92rem',
    fontWeight: 600,
    color: colors.text,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  pausedPill: {
    fontSize: '0.62rem',
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
    backgroundColor: colors.surface2,
    paddingBlock: 2,
    paddingInline: 7,
    borderRadius: 999,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    flexShrink: 0,
  },
  desc: {
    fontSize: '0.76rem',
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
    fontSize: '0.72rem',
    color: colors.textDim,
    whiteSpace: 'nowrap',
  },
  metaNext: {color: colors.accent},
  metaNever: {fontStyle: 'italic'},
  metaStats: {fontSize: '0.66rem', letterSpacing: '0.04em'},

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
    borderColor: {default: colors.border, ':hover': `rgba(${channels.tint}, 0.15)`},
    color: {default: colors.textDim, ':hover': colors.text},
    borderRadius: 6,
    padding: 6,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    cursor: 'pointer',
    transition: 'background 0.12s, color 0.12s, border-color 0.12s',
  },
  actionPlay: {
    backgroundColor: {default: colors.surface, ':hover': `rgba(${channels.accent}, 0.15)`},
    borderColor: {default: colors.border, ':hover': `rgba(${channels.accent}, 0.4)`},
    color: {default: colors.textDim, ':hover': colors.accent},
  },
  actionDanger: {
    backgroundColor: {default: colors.surface, ':hover': `rgba(${channels.danger}, 0.12)`},
    borderColor: {default: colors.border, ':hover': `rgba(${channels.danger}, 0.4)`},
    color: {default: colors.textDim, ':hover': colors.danger},
  },

  toggle: {
    display: 'inline-flex',
    alignItems: 'center',
    width: 26,
    height: 14,
    borderRadius: 999,
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
    borderRadius: 8,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    flexShrink: 0,
  },
  prompt: {
    color: colors.accent,
    backgroundColor: colors.accentDim,
    borderColor: `rgba(${channels.accent}, 0.25)`,
  },
  code: {
    color: colors.ok,
    backgroundColor: `rgba(${channels.ok}, 0.1)`,
    borderColor: `rgba(${channels.ok}, 0.25)`,
  },
  webhook: {
    color: colors.webhookText,
    backgroundColor: colors.webhookBg,
    borderColor: 'rgba(196, 181, 253, 0.25)',
  },
  monitor: {
    color: colors.warn,
    backgroundColor: `rgba(${channels.warn}, 0.1)`,
    borderColor: `rgba(${channels.warn}, 0.25)`,
  },
});

/** Input-type and schedule chips under the name. */
export const chip = stylex.create({
  base: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    fontSize: '0.64rem',
    fontWeight: 500,
    paddingBlock: 2,
    paddingInline: 7,
    borderRadius: 4,
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
  adhoc: {
    backgroundColor: 'transparent',
    color: colors.textDim,
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.border,
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
    backgroundImage: `linear-gradient(180deg, rgba(${channels.tint}, 0.03), rgba(${channels.tint}, 0.005))`,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 16,
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
  title: {margin: 0, fontSize: '1.05rem', fontWeight: 600, color: colors.text},
  body: {margin: 0, color: colors.textDim, fontSize: '0.82rem', lineHeight: 1.5},
});

/** The slide-in editor sheet. */
export const panel = stylex.create({
  backdrop: {
    position: 'fixed',
    inset: 0,
    // A full-viewport blur is the one backdrop-filter cost worth caring about
    // on a mid-range phone; the scrim already carries the separation, so touch
    // trades the blur for a heavier tint.
    backgroundColor: {
      default: `rgba(${channels.shadow}, 0.6)`,
      '@media (hover: none)': `rgba(${channels.shadow}, 0.72)`,
    },
    backdropFilter: {default: 'blur(4px)', '@media (hover: none)': 'none'},
    WebkitBackdropFilter: {default: 'blur(4px)', '@media (hover: none)': 'none'},
    zIndex: 42,
  },
  root: {
    position: 'fixed',
    insetBlock: 0,
    insetInlineEnd: 0,
    width: {default: 440, '@media (max-width: 860px)': '100%'},
    maxWidth: {default: null, '@media (max-width: 860px)': '100%'},
    minWidth: {default: null, '@media (max-width: 860px)': 0},
    paddingBlockStart: {default: null, '@media (max-width: 860px)': css.safeTop},
    paddingBlockEnd: {default: null, '@media (max-width: 860px)': css.safeBottom},
    backgroundColor: {default: colors.glassBg, '@media (max-width: 860px)': colors.bg},
    backdropFilter: {default: layout.blur, '@media (max-width: 860px)': 'none'},
    WebkitBackdropFilter: {default: layout.blur, '@media (max-width: 860px)': 'none'},
    borderInlineStartWidth: {default: 1, '@media (max-width: 860px)': 0},
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.glassBorder,
    boxShadow: `-4px 0 28px rgba(${channels.shadow}, 0.45)`,
    display: 'flex',
    flexDirection: 'column',
    zIndex: 52,
    animationName: kf.slideIn,
    animationDuration: '0.22s',
    animationTimingFunction: 'ease',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBlock: 14,
    paddingInline: 16,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    fontSize: '0.85rem',
    fontWeight: 600,
    flexShrink: 0,
  },
  body: {
    flex: 1,
    overflowY: 'auto',
    padding: 0,
    '::-webkit-scrollbar': {width: 4},
    '::-webkit-scrollbar-thumb': {backgroundColor: colors.border, borderRadius: 2},
  },
});

/** A red note under a destructive confirm dialog's body. */
export const confirmWarn = stylex.create({
  base: {color: colors.danger, fontSize: '0.78rem'},
});
