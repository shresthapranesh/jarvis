import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, css, layout, type} from '../theme/tokens.stylex';

/* ── Styles for AutomationRunsPanel.tsx ────────────────────────────────
   The run-history sheet that slides in over the automation list: its
   chrome, the live card an in-flight run streams into, and the timeline
   of finished runs below it.

   The backdrop, the type icon and the status dot are the same objects the
   list behind this panel uses, so they are imported from
   `routes/automation.styles` rather than restated here. */

/** The sheet. Wider than the editor panel — a run's output needs the room. */
export const sheet = stylex.create({
  root: {
    position: 'fixed',
    insetBlock: 0,
    insetInlineEnd: 0,
    width: {default: 'min(820px, 65vw)', '@media (max-width: 860px)': '100%'},
    minWidth: {default: 480, '@media (max-width: 860px)': 0},
    maxWidth: {default: null, '@media (max-width: 860px)': '100%'},
    paddingBlockStart: {default: null, '@media (max-width: 860px)': css.safeTop},
    paddingBlockEnd: {default: null, '@media (max-width: 860px)': css.safeBottom},
    zIndex: 100,
    // Translucent reads well for a narrow panel floating over a wide page. At
    // full width there is no "over" left — the list behind shows through the
    // text — and a full-screen backdrop-filter costs a phone a frame.
    backgroundColor: {default: colors.glassBg, '@media (max-width: 860px)': colors.bg},
    backdropFilter: {default: layout.blur, '@media (max-width: 860px)': 'none'},
    WebkitBackdropFilter: {default: layout.blur, '@media (max-width: 860px)': 'none'},
    borderInlineStartWidth: {default: 1, '@media (max-width: 860px)': 0},
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.glassBorder,
    boxShadow: `-10px 0 40px rgba(${channels.shadow}, 0.4)`,
    display: 'flex',
    flexDirection: 'column',
    animationName: kf.slideIn,
    animationDuration: '0.22s',
    animationTimingFunction: 'ease-out',
  },

  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 10,
    paddingBlock: 14,
    paddingInline: 16,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  title: {display: 'flex', alignItems: 'center', gap: 10, minWidth: 0, flex: 1},
  /** The list's type icon, one size down to fit the header row. */
  typeIconSm: {width: 28, height: 28, borderRadius: 7},
  name: {
    fontSize: '0.92rem',
    fontWeight: 600,
    color: colors.text,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  actions: {display: 'flex', alignItems: 'center', gap: 5, flexShrink: 0},

  body: {
    flex: 1,
    overflowY: 'auto',
    paddingBlockStart: 14,
    paddingBlockEnd: 24,
    paddingInline: 16,
    display: 'flex',
    flexDirection: 'column',
    gap: 14,
  },
  sectionLabel: {
    fontSize: '0.66rem',
    textTransform: 'uppercase',
    letterSpacing: '0.08em',
    color: colors.textDim,
    fontWeight: 600,
    paddingBlock: 4,
    display: 'flex',
    alignItems: 'center',
    gap: 7,
  },
  count: {color: colors.textDim, fontWeight: 400, letterSpacing: '0.04em'},

  empty: {
    color: colors.textDim,
    fontSize: '0.82rem',
    paddingBlock: 30,
    paddingInline: 10,
    textAlign: 'center',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: 8,
  },
  emptyP: {margin: 0},
});

/** The header buttons. Smaller and denser than the shared `btn`. */
export const headBtn = stylex.create({
  base: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    backgroundColor: {default: colors.surface, ':hover:not(:disabled)': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    color: {default: colors.textDim, ':hover:not(:disabled)': colors.text},
    paddingBlock: 6,
    paddingInline: 8,
    borderRadius: 6,
    fontFamily: 'inherit',
    fontSize: '0.74rem',
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.5},
    transition: 'background 0.12s, color 0.12s, border-color 0.12s',
  },
  primary: {
    backgroundImage: `linear-gradient(135deg, ${colors.accentStrong}, ${colors.accent})`,
    color: {default: colors.accentContrast, ':hover:not(:disabled)': '#fff'},
    borderColor: 'transparent',
    paddingInline: 10,
    fontWeight: 500,
    boxShadow: {
      default: null,
      ':hover:not(:disabled)': `0 2px 10px rgba(${channels.accent}, 0.35)`,
    },
  },
  close: {paddingInline: 6},
});

/** The in-flight run: a live-tailing card that fades itself out when done. */
export const live = stylex.create({
  card: {
    position: 'relative',
    borderRadius: 10,
    backgroundColor: `rgba(${channels.tint}, 0.03)`,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    overflow: 'hidden',
    transition: 'opacity 0.35s ease',
  },
  // A gradient that sweeps around the edge while the run streams. The border
  // goes transparent and a ::before one pixel larger shows through it.
  cardRunning: {
    borderColor: 'transparent',
    backgroundClip: 'padding-box',
    '::before': {
      content: '',
      position: 'absolute',
      inset: -1,
      borderRadius: 10,
      backgroundImage: `linear-gradient(120deg, rgba(${channels.accent}, 0), rgba(${channels.accent}, 0.7), rgba(127, 220, 201, 0.7), rgba(${channels.accent}, 0))`,
      backgroundSize: '200% 100%',
      animationName: kf.glowSweep,
      animationDuration: '2.4s',
      animationTimingFunction: 'linear',
      animationIterationCount: 'infinite',
      zIndex: -1,
      pointerEvents: 'none',
    },
  },
  cardFading: {opacity: 0},

  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBlock: 8,
    paddingInline: 12,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    backgroundColor: `rgba(${channels.shadow}, 0.15)`,
  },
  pill: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    fontSize: '0.7rem',
    fontWeight: 500,
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    paddingBlock: 3,
    paddingInline: 9,
    borderRadius: 999,
  },
  pillRunning: {backgroundColor: colors.accentDim, color: colors.accent},
  pillDone: {backgroundColor: `rgba(${channels.ok}, 0.15)`, color: colors.ok},
  pillError: {backgroundColor: `rgba(${channels.danger}, 0.15)`, color: colors.danger},
  dot: {
    width: 6,
    height: 6,
    borderRadius: '50%',
    backgroundColor: 'currentColor',
    animationName: kf.railPulse,
    animationDuration: '1.4s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  timer: {
    fontSize: '0.72rem',
    fontFamily: type.mono,
    color: colors.textDim,
    letterSpacing: '0.04em',
  },

  body: {
    maxHeight: 'min(520px, 50vh)',
    minHeight: 120,
    overflowY: 'auto',
    paddingBlock: 14,
    paddingInline: 16,
    fontSize: '0.86rem',
    lineHeight: 1.6,
    color: colors.text,
  },
  thinking: {display: 'flex', justifyContent: 'center', paddingBlock: 18},
});

/** The history below the live card — one expandable row per finished run. */
export const timeline = stylex.create({
  list: {display: 'flex', flexDirection: 'column', gap: 6},

  row: {
    backgroundColor: `rgba(${channels.tint}, 0.025)`,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': `rgba(${channels.tint}, 0.16)`},
    borderRadius: 8,
    overflow: 'hidden',
    transition: 'border-color 0.12s',
  },
  rowOpen: {borderColor: `rgba(${channels.accent}, 0.35)`},

  head: {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    paddingBlock: 10,
    paddingInline: 14,
    cursor: 'pointer',
    userSelect: 'none',
  },
  meta: {display: 'flex', alignItems: 'center', gap: 9, flexWrap: 'wrap'},

  status: {
    fontSize: '0.66rem',
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    fontWeight: 600,
    paddingBlock: 2,
    paddingInline: 7,
    borderRadius: 999,
    backgroundColor: colors.surface2,
    color: colors.textDim,
  },
  statusDone: {backgroundColor: `rgba(${channels.ok}, 0.12)`, color: colors.ok},
  statusError: {backgroundColor: `rgba(${channels.danger}, 0.14)`, color: colors.danger},
  statusRunning: {backgroundColor: colors.accentDim, color: colors.accent},

  trigger: {color: colors.textDim, display: 'inline-flex'},
  time: {fontSize: '0.76rem', color: colors.textDim, whiteSpace: 'nowrap'},

  duration: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    minWidth: 0,
    marginInlineStart: 'auto',
  },
  // Width is set inline — it is a proportion of the slowest run in the list.
  bar: {
    height: 4,
    borderRadius: 2,
    minWidth: 12,
    maxWidth: 120,
    backgroundColor: colors.textDim,
    display: 'inline-block',
  },
  barDone: {
    backgroundImage: `linear-gradient(90deg, rgba(${channels.ok}, 0.3), ${colors.ok})`,
  },
  barError: {
    backgroundImage: `linear-gradient(90deg, rgba(${channels.danger}, 0.3), ${colors.danger})`,
  },
  barRunning: {
    backgroundImage: `linear-gradient(90deg, rgba(${channels.accent}, 0.3), ${colors.accent})`,
  },
  barLabel: {fontSize: '0.7rem', color: colors.textDim, fontFamily: type.mono},

  chevron: {
    color: colors.textDim,
    display: 'inline-flex',
    transition: 'transform 0.18s',
  },
  chevronOpen: {transform: 'rotate(180deg)'},

  snippet: {
    lineHeight: 1.55,
    color: colors.text,
    opacity: 0.78,
    display: '-webkit-box',
    WebkitLineClamp: 2,
    WebkitBoxOrient: 'vertical',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    fontFamily: type.mono,
    fontSize: '0.74rem',
    paddingInlineStart: 1,
  },
  snippetError: {color: colors.errorText, opacity: 0.85},

  output: {
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    paddingBlock: 14,
    paddingInline: 16,
    backgroundColor: `rgba(${channels.shadow}, 0.18)`,
    fontSize: '0.85rem',
    lineHeight: 1.6,
    color: colors.text,
    maxHeight: 'min(560px, 55vh)',
    overflowY: 'auto',
  },
  outputError: {backgroundColor: colors.errorBg, color: colors.errorText},
});
