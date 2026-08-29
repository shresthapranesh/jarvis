import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {bp, channels, colors, layout, radii, space, type} from '../theme/tokens.stylex';

/* ── Styles for RunSpine.tsx ───────────────────────────────────────────
   The fixed rail, the hairline track of nodes hung off it, and the stats
   footer. The rail is a trace, not a log — everything here is sized for
   a 208px column.

   At rest it is not that column. A settled run's trace is worth a glance and
   not 208px of reading width, so once the run finishes the rail drops to
   `spineCollapsedW` and shows marks only; hover or click brings the full
   column back. Collapsed and expanded are separate renders rather than one
   clipped by `overflow`, because a 208px layout squeezed into 30px rewraps
   into something that looks broken for the length of the transition. */

/** The fixed column and its header. */
export const rail = stylex.create({
  root: {
    position: 'fixed',
    insetBlock: 0,
    insetInlineEnd: 0,
    width: layout.spineW,
    transition: 'width 0.2s cubic-bezier(0.2, 0.8, 0.2, 1)',
    display: {default: 'flex', [bp.wide]: 'none'},
    flexDirection: 'column',
    borderInlineStartWidth: 1,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.glassBorder,
    backgroundImage: `linear-gradient(180deg, rgba(${channels.tint}, 0.035) 0%, rgba(${channels.tint}, 0.015) 100%)`,
    backdropFilter: layout.blur,
    WebkitBackdropFilter: layout.blur,
    zIndex: 20,
    animationName: kf.panelEnter,
    animationDuration: '0.24s',
    animationTimingFunction: 'cubic-bezier(0.2, 0.8, 0.2, 1)',
    animationFillMode: 'both',
  },
  /** At rest: marks only. Expands on hover, or on click for touch. */
  rootCollapsed: {
    width: layout.spineCollapsedW,
    cursor: 'pointer',
    alignItems: 'center',
    // The rail is a peripheral thing when there is nothing running; it should
    // not compete with the thread for attention until asked.
    opacity: {default: 0.65, ':hover': 1},
    transition: 'width 0.2s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.2s ease',
  },
  head: {
    display: 'flex',
    alignItems: 'baseline',
    justifyContent: 'space-between',
    paddingBlock: `${space.s4} ${space.s3}`,
    paddingInline: space.s4,
    flexShrink: 0,
  },
  eyebrow: {
    fontSize: type.tMicro,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: type.trackMicro,
    color: colors.textDim,
  },
  state: {fontFamily: type.mono, fontSize: type.tMicro, color: colors.textFaint},
  stateLive: {color: colors.signalLive},

  /* The rail itself: one hairline, nodes hung off it. */
});

/** The scrolling list of nodes and the hairline connecting them. */
export const track = stylex.create({
  root: {
    flex: 1,
    overflowY: 'auto',
    paddingBlock: `0 ${space.s3}`,
    paddingInlineStart: space.s4,
    paddingInlineEnd: space.s3,
    position: 'relative',
    scrollbarWidth: 'none',
    '::-webkit-scrollbar': {display: 'none'},
  },
  // The connecting hairline lives on this wrapper, not the scroll container, so
  // it ends at the last node instead of dangling.
  nodes: {
    position: 'relative',
    '::before': {
      content: '',
      position: 'absolute',
      insetInlineStart: 4,
      insetBlockStart: 10,
      insetBlockEnd: 10,
      width: 1,
      backgroundColor: `rgba(${channels.tint}, 0.14)`,
    },
  },
  empty: {
    fontSize: type.tSmall,
    color: colors.textFaint,
    paddingBlock: space.s2,
    paddingInlineStart: space.s4,
  },
  earlier: {
    display: 'block',
    width: '100%',
    textAlign: 'left',
    backgroundColor: 'transparent',
    borderStyle: 'none',
    cursor: 'pointer',
    fontFamily: type.mono,
    fontSize: type.tMicro,
    color: {default: colors.textFaint, ':hover': colors.accent},
    paddingBlock: `${space.s1} ${space.s2}`,
    paddingInlineStart: space.s4,
    transition: 'color 0.15s',
  },
});

/** The at-rest rail: the same marks, the same hairline, no labels. */
export const mini = stylex.create({
  track: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 9,
    paddingBlock: space.s4,
    position: 'relative',
    overflow: 'hidden',
    // The same hairline the expanded track draws, minus the node offsets.
    '::before': {
      content: '',
      position: 'absolute',
      insetBlockStart: 20,
      insetBlockEnd: 20,
      width: 1,
      backgroundColor: `rgba(${channels.tint}, 0.14)`,
    },
  },
  count: {
    flexShrink: 0,
    fontFamily: type.mono,
    fontSize: type.tNano,
    color: colors.textFaint,
    paddingBlock: space.s3,
    textAlign: 'center',
  },
});

/** One step on the rail. Kind is carried by the reserved signal hues. */
export const node = stylex.create({
  root: {
    position: 'relative',
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    width: '100%',
    backgroundColor: 'transparent',
    borderStyle: 'none',
    cursor: 'pointer',
    textAlign: 'left',
    paddingBlock: 4,
    paddingInline: 0,
    color: {default: colors.textDim, ':hover': colors.text},
    transition: 'color 0.15s',
  },
  pending: {cursor: 'default', color: colors.signalLive},
  mark: {
    position: 'relative',
    zIndex: 1,
    width: 9,
    height: 9,
    flexShrink: 0,
    borderRadius: '50%',
    backgroundColor: colors.bg,
    borderWidth: 1.5,
    borderStyle: 'solid',
    borderColor: 'currentColor',
    boxShadow: `0 0 0 3px ${colors.bg}`,
  },
  /* Kind is carried by the reserved signal hues — the whole reason they exist. */
  markTool: {color: colors.signalTool},
  markWorker: {color: colors.signalInsight},
  markArtifact: {color: colors.accent},
  markThink: {color: colors.textFaint},
  markActive: {
    color: colors.signalLive,
    backgroundColor: colors.signalLive,
    animationName: kf.spinePing,
    animationDuration: '1.4s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  label: {
    fontFamily: type.mono,
    fontSize: type.tMicro,
    letterSpacing: '-0.01em',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
});

/** Run totals and the control that opens the full sidebar. */
export const foot = stylex.create({
  root: {
    flexShrink: 0,
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    paddingBlock: space.s3,
    paddingInline: space.s4,
    display: 'flex',
    flexDirection: 'column',
    gap: space.s2,
  },
  stats: {display: 'flex', flexWrap: 'wrap', rowGap: space.s1, columnGap: space.s3},
  stat: {display: 'flex', alignItems: 'baseline', gap: 5},
  key: {
    fontSize: type.tMicro,
    textTransform: 'uppercase',
    letterSpacing: type.trackMicro,
    color: colors.textFaint,
  },
  value: {fontFamily: type.mono, fontSize: type.tSmall, color: colors.text},
  valueWorkers: {color: colors.signalInsight},
  expand: {
    backgroundColor: {default: 'transparent', ':hover': `rgba(${channels.tint}, 0.04)`},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.accentDim},
    borderRadius: radii.sm,
    color: {default: colors.textDim, ':hover': colors.accent},
    fontSize: type.tSmall,
    paddingBlock: 5,
    paddingInline: space.s2,
    cursor: 'pointer',
    transition: 'color 0.15s, border-color 0.15s, background 0.15s',
  },
});
