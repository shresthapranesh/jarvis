import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, css, layout, type} from '../theme/tokens.stylex';

/* ── Styles for ActivitySidebar.tsx ────────────────────────────────────
   The panel frame, the step rows it lists, the worker groups those rows
   fold into, and the budget box pinned above them. */

/** The sheet itself — a side panel on a desktop, the whole screen below 860px. */
export const panel = stylex.create({
  root: {
    position: 'fixed',
    insetBlock: 0,
    insetInlineEnd: 0,
    // Below the breakpoint this stops being a panel floating over the thread
    // and becomes the whole screen — at which point the glass has nothing to
    // sit over, the text reads through it, and a full-viewport backdrop-filter
    // is a per-frame cost mid-range phones actually pay.
    width: {default: layout.rightW, '@media (max-width: 860px)': '100%'},
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
    boxShadow: `-4px 0 22px rgba(${channels.shadow}, 0.32), inset 1px 0 0 rgba(${channels.tint}, 0.04)`,
    display: 'flex',
    flexDirection: 'column',
    zIndex: 50,
    animationName: kf.panelEnter,
    animationDuration: '0.2s',
    animationTimingFunction: 'cubic-bezier(0.2, 0.8, 0.2, 1)',
    animationFillMode: 'forwards',
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
    flexShrink: 0,
  },
  title: {display: 'flex', alignItems: 'center', gap: 8, fontSize: '0.85rem', fontWeight: 600},
  live: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    fontSize: '0.68rem',
    fontWeight: 500,
    color: colors.ok,
    animationName: kf.livePulse,
    animationDuration: '1.6s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  body: {
    flex: 1,
    overflowY: 'auto',
    paddingBlock: 8,
    '::-webkit-scrollbar': {width: 4},
    '::-webkit-scrollbar-thumb': {
      backgroundColor: `rgba(${channels.tint}, 0.12)`,
      borderRadius: 2,
    },
  },
});

/** One recorded step, expandable to its raw payload. */
export const row = stylex.create({
  root: {
    display: 'flex',
    flexDirection: 'column',
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    animationName: kf.stepEnter,
    animationDuration: '0.18s',
    animationTimingFunction: 'ease',
    animationFillMode: 'forwards',
    ':first-of-type': {borderBlockStartStyle: 'none'},
  },
  nested: {':first-of-type': {borderBlockStartStyle: 'none'}},
  summary: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: 8,
    paddingInline: 16,
    fontSize: '0.8rem',
    userSelect: 'none',
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
  },
  clickable: {cursor: 'pointer'},
  plain: {cursor: 'default'},
  /** Rows inside a worker group sit closer to the rail. */
  inset: {paddingInlineStart: 10},
  chevron: {
    marginInlineStart: 'auto',
    color: colors.textDim,
    transition: 'transform 0.18s ease',
    flexShrink: 0,
  },
  chevronOpen: {transform: 'rotate(90deg)'},
  chevronFlush: {marginInlineStart: 0},
  detail: {paddingBlock: '0 10px', paddingInline: 16},
  pre: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 6,
    paddingBlock: 10,
    paddingInline: 12,
    fontSize: '0.72rem',
    fontFamily: type.mono,
    overflowX: 'auto',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-all',
    color: colors.text,
    lineHeight: 1.5,
  },
  source: {
    fontSize: '0.67rem',
    paddingBlock: 1,
    paddingInline: 6,
    borderRadius: 4,
    flexShrink: 0,
    fontWeight: 500,
  },
  sourceMain: {backgroundColor: colors.surface2, color: colors.textDim},
  sourceSub: {backgroundColor: colors.accentDim, color: colors.accent},
  node: {color: colors.text, wordBreak: 'break-word'},
});

/** A spawned worker's steps, folded into one collapsible block. */
export const group = stylex.create({
  root: {
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    animationName: kf.stepEnter,
    animationDuration: '0.18s',
    animationTimingFunction: 'ease',
    animationFillMode: 'forwards',
    ':first-of-type': {borderBlockStartStyle: 'none'},
  },
  head: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    paddingBlock: 8,
    paddingInline: 16,
    fontSize: '0.8rem',
    cursor: 'pointer',
    userSelect: 'none',
    minWidth: 0,
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
  },
  count: {
    flex: 'none',
    marginInlineStart: 'auto',
    fontSize: '0.68rem',
    color: colors.textDim,
    backgroundColor: colors.surface2,
    borderRadius: 8,
    paddingInline: 6,
  },
});

/** Token spend and throughput for the run. */
export const budget = stylex.create({
  box: {
    paddingBlock: 8,
    paddingInline: 12,
    marginBlock: '0 8px',
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 8,
    fontSize: '0.78rem',
  },
  head: {display: 'flex', justifyContent: 'space-between', marginBlockEnd: 4},
  ok: {color: colors.textDim},
  warn: {color: colors.warningText},
  over: {color: colors.errorText},
  bar: {
    height: 4,
    backgroundColor: colors.surface2,
    borderRadius: 4,
    overflow: 'hidden',
    marginBlockEnd: 6,
  },
  fill: {height: '100%', backgroundColor: colors.accent, transition: 'width 0.3s'},
  fillOver: {backgroundColor: colors.errorText},
  stats: {display: 'flex', gap: 12, color: colors.textDim},
  perf: {
    marginBlockStart: 4,
    paddingBlockStart: 4,
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    fontVariantNumeric: 'tabular-nums',
  },
});

/** Nothing recorded yet. */
export const empty = stylex.create({
  block: {
    paddingBlock: 32,
    paddingInline: 16,
    textAlign: 'center',
    color: colors.textDim,
    fontSize: '0.82rem',
  },
  inline: {padding: 10, textAlign: 'left'},
});
