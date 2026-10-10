import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, type} from '../theme/tokens.stylex';

/* ── Styles for AutomationDetailPage.tsx and AutomationRunPage.tsx ─────
   One automation as a page: a header with its identity and actions, its
   configuration, then the list of runs. A run opens as its own page under
   it, with the full output (streaming, while the run is live).

   The type icon, chips and status dot are the list's own objects, imported
   from `routes/automation.styles` rather than restated here. */

/** The frame both pages share: a fixed header over a scrolling body. */
export const detail = stylex.create({
  header: {
    paddingBlock: {default: '18px 16px', '@media (max-width: 860px)': '12px 12px'},
    paddingInline: {default: 28, '@media (max-width: 860px)': 16},
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  },
  back: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 4,
    alignSelf: 'flex-start',
    maxWidth: '100%',
    fontSize: type.tSmall,
    color: {default: colors.textDim, ':hover': colors.text},
    textDecoration: 'none',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    transition: 'color 0.12s',
  },
  titleRow: {
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    gap: 16,
    flexWrap: {default: null, '@media (max-width: 768px)': 'wrap'},
  },
  identity: {display: 'flex', alignItems: 'flex-start', gap: 12, minWidth: 0, flex: '1 1 0'},
  titleBlock: {display: 'flex', flexDirection: 'column', gap: 5, minWidth: 0},
  titleLine: {display: 'flex', alignItems: 'center', gap: 8, minWidth: 0},
  title: {
    margin: 0,
    fontSize: type.tPage,
    fontWeight: 600,
    letterSpacing: '-0.01em',
    color: colors.text,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  desc: {margin: 0, fontSize: type.tUi, color: colors.textDim, lineHeight: 1.5},
  badges: {display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap'},
  meta: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    fontSize: type.tSmall,
    color: colors.textDim,
    whiteSpace: 'nowrap',
  },
  metaAccent: {color: colors.accent},
  metaLink: {
    color: {default: colors.textDim, ':hover': colors.text},
    textDecoration: {default: 'none', ':hover': 'underline'},
  },
  actions: {display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0},

  body: {
    flex: 1,
    overflowY: 'auto',
    paddingBlock: {default: '18px 60px', '@media (max-width: 860px)': '12px 60px'},
    paddingInline: {default: 28, '@media (max-width: 860px)': 16},
    display: 'flex',
    flexDirection: 'column',
    gap: 22,
  },
  section: {display: 'flex', flexDirection: 'column', gap: 8},
  sectionLabel: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    fontSize: type.tNano,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.09em',
    color: colors.textDim,
    paddingInline: 2,
  },
  count: {fontWeight: 400, letterSpacing: '0.04em'},
  sectionToggle: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    backgroundColor: 'transparent',
    borderStyle: 'none',
    padding: 0,
    fontFamily: 'inherit',
    fontSize: 'inherit',
    fontWeight: 'inherit',
    letterSpacing: 'inherit',
    textTransform: 'inherit',
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
  },
  chevron: {display: 'inline-flex', transition: 'transform 0.18s', transform: 'rotate(-90deg)'},
  chevronOpen: {transform: 'rotate(0deg)'},

  /** The automation's prompt / code / request, read-only. */
  config: {
    margin: 0,
    paddingBlock: 12,
    paddingInline: 14,
    backgroundColor: `rgba(${channels.shadow}, 0.18)`,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    fontFamily: type.mono,
    fontSize: type.tSmall,
    lineHeight: 1.6,
    color: colors.text,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    maxHeight: 320,
    overflowY: 'auto',
  },

  empty: {
    color: colors.textDim,
    fontSize: type.tUi,
    paddingBlock: 30,
    paddingInline: 10,
    textAlign: 'center',
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    gap: 8,
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.border,
    borderRadius: 3,
  },
  emptyP: {margin: 0},
});

/** The paused/enabled switch in the detail header. */
export const toggle = stylex.create({
  track: {
    display: 'inline-flex',
    alignItems: 'center',
    width: 26,
    height: 14,
    borderRadius: 2,
    padding: 2,
    transition: 'background 0.15s',
  },
  on: {backgroundColor: colors.accent, justifyContent: 'flex-end'},
  off: {backgroundColor: colors.border, justifyContent: 'flex-start'},
  dot: {width: 10, height: 10, borderRadius: '50%', backgroundColor: '#fff', display: 'block'},
});

/** One row per run in the detail page's list; the whole row is the link. */
export const runRow = stylex.create({
  list: {display: 'flex', flexDirection: 'column', gap: 6},
  root: {
    display: 'grid',
    gridTemplateColumns: {
      default: '96px 20px 120px minmax(0, 1fr) 150px 14px',
      // On a phone the snippet and the duration bar are the first to go.
      '@media (max-width: 700px)': '88px 20px minmax(0, 1fr) 14px',
    },
    alignItems: 'center',
    gap: 12,
    paddingBlock: 10,
    paddingInline: 14,
    backgroundColor: {
      default: `rgba(${channels.tint}, 0.025)`,
      ':hover': `rgba(${channels.accent}, 0.04)`,
    },
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': `rgba(${channels.accent}, 0.35)`},
    borderRadius: 3,
    color: colors.text,
    textDecoration: 'none',
    outline: {default: null, ':focus-visible': `2px solid ${colors.accent}`},
    outlineOffset: 2,
    transition: 'border-color 0.12s, background 0.12s',
  },
  status: {display: 'flex', alignItems: 'center', gap: 8, minWidth: 0},
  trigger: {color: colors.textDim, display: 'inline-flex'},
  time: {fontSize: type.tSmall, color: colors.textDim, whiteSpace: 'nowrap'},
  snippet: {
    display: {default: 'block', '@media (max-width: 700px)': 'none'},
    fontFamily: type.mono,
    fontSize: type.tSmall,
    color: colors.text,
    opacity: 0.75,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  snippetError: {color: colors.errorText, opacity: 0.85},
  duration: {
    display: {default: 'flex', '@media (max-width: 700px)': 'none'},
    alignItems: 'center',
    justifyContent: 'flex-end',
    gap: 6,
    minWidth: 0,
  },
  arrow: {color: colors.textDim, display: 'inline-flex', transform: 'rotate(180deg)'},
});

/** The run's status as a small uppercase pill. */
export const pill = stylex.create({
  base: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    fontSize: type.tMicro,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    paddingBlock: 2,
    paddingInline: 7,
    borderRadius: 2,
    backgroundColor: colors.surface2,
    color: colors.textDim,
    whiteSpace: 'nowrap',
  },
  done: {backgroundColor: `rgba(${channels.ok}, 0.12)`, color: colors.ok},
  error: {backgroundColor: `rgba(${channels.danger}, 0.14)`, color: colors.danger},
  running: {backgroundColor: colors.accentDim, color: colors.accent},
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
});

/** A run's duration as a bar, proportional to the slowest run listed. */
export const bar = stylex.create({
  // Width is set inline.
  base: {
    height: 4,
    borderRadius: 2,
    minWidth: 8,
    maxWidth: 90,
    backgroundColor: colors.textDim,
    display: 'inline-block',
  },
  done: {backgroundImage: `linear-gradient(90deg, rgba(${channels.ok}, 0.3), ${colors.ok})`},
  error: {
    backgroundImage: `linear-gradient(90deg, rgba(${channels.danger}, 0.3), ${colors.danger})`,
  },
  running: {
    backgroundImage: `linear-gradient(90deg, rgba(${channels.accent}, 0.3), ${colors.accent})`,
  },
  label: {
    fontSize: type.tMicro,
    color: colors.textDim,
    fontFamily: type.mono,
    whiteSpace: 'nowrap',
  },
});

/** The run page: its facts line and the output below. */
export const run = stylex.create({
  facts: {display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap'},
  nav: {display: 'flex', alignItems: 'center', gap: 4},
  output: {
    maxWidth: 820,
    width: '100%',
    fontSize: type.tBody,
    lineHeight: 1.65,
    color: colors.text,
    wordBreak: 'break-word',
  },
  thinking: {display: 'flex', paddingBlock: 18},
  timer: {fontFamily: type.mono, letterSpacing: '0.04em'},
});
