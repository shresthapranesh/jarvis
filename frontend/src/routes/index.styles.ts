import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, radii, space, type} from '../theme/tokens.stylex';

/* ── Styles for routes/index.tsx ───────────────────────────────────────
   The dispatch screen: a greeting, the composer as hero, and two short
   lists — what is running now, and what was asked recently. */

/** The centred column and its header. */
export const dispatch = stylex.create({
  scroll: {
    flex: 1,
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
    justifyContent: 'center',
    paddingBlock: space.s7,
    paddingInline: space.s5,
  },
  inner: {
    width: '100%',
    maxWidth: 680,
    marginInline: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: space.s5,
    animationName: kf.dispatchEnter,
    animationDuration: '0.4s',
    animationTimingFunction: 'cubic-bezier(0.32, 0.72, 0, 1)',
    animationFillMode: 'both',
  },
  head: {display: 'flex', flexDirection: 'column', gap: space.s2},
  greeting: {
    fontFamily: type.display,
    fontSize: type.tDisplay,
    // A serif at 500 with near-normal tracking. The -0.035em the grotesk
    // wanted closes a serif's counters and turns it into a smear.
    fontWeight: 500,
    letterSpacing: '-0.005em',
    lineHeight: 1.05,
    color: colors.text,
  },
  sub: {
    display: 'flex',
    alignItems: 'center',
    gap: space.s2,
    fontSize: type.tUi,
    color: colors.textDim,
  },
  pulse: {
    width: 6,
    height: 6,
    borderRadius: '50%',
    backgroundColor: colors.signalLive,
    flexShrink: 0,
    animationName: kf.brandPulse,
    animationDuration: '1.6s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  error: {margin: 0},
  section: {display: 'flex', flexDirection: 'column', gap: space.s2},
  heading: {
    fontSize: type.tMicro,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: type.trackMicro,
    color: colors.textFaint,
  },
  more: {
    alignSelf: 'flex-start',
    fontSize: type.tSmall,
    color: {default: colors.textDim, ':hover': colors.accent},
    textDecoration: 'none',
    paddingBlock: space.s1,
    paddingInline: space.s3,
    transition: 'color 0.15s',
  },
});

/** The "recent conversations" rows. */
export const recent = stylex.create({
  list: {listStyle: 'none', display: 'flex', flexDirection: 'column'},
  row: {
    display: 'flex',
    alignItems: 'baseline',
    justifyContent: 'space-between',
    gap: space.s4,
    paddingBlock: space.s2,
    paddingInline: space.s3,
    borderRadius: radii.sm,
    textDecoration: 'none',
    borderInlineStartWidth: 2,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: {default: 'transparent', ':hover': colors.accent},
    backgroundColor: {default: 'transparent', ':hover': `rgba(${channels.tint}, 0.05)`},
    transition:
      'background 0.18s cubic-bezier(0.2, 0.8, 0.2, 1), border-color 0.18s cubic-bezier(0.2, 0.8, 0.2, 1)',
    // The title brightens with the row, which no selector on the title can see.
    '--recent-title-color': {default: colors.textDim, ':hover': colors.text},
  },
  title: {
    flex: 1,
    minWidth: 0,
    fontSize: type.tUi,
    color: 'var(--recent-title-color)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  meta: {
    display: 'flex',
    alignItems: 'center',
    gap: 5,
    flexShrink: 0,
    fontFamily: type.mono,
    fontSize: type.tMicro,
    color: colors.textFaint,
  },
  dot: {opacity: 0.5},
});

/** A run currently in flight. */
export const run = stylex.create({
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: space.s3,
    paddingBlock: space.s2,
    paddingInline: space.s3,
    borderRadius: radii.sm,
    fontSize: type.tUi,
    borderInlineStartWidth: 2,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.signalLive,
    backgroundColor: colors.signalLiveDim,
    marginBlockEnd: 2,
  },
  mark: {
    width: 5,
    height: 5,
    borderRadius: '50%',
    backgroundColor: colors.signalLive,
    flexShrink: 0,
    animationName: kf.brandPulse,
    animationDuration: '1.6s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  label: {
    flex: 1,
    minWidth: 0,
    color: colors.text,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  time: {fontFamily: type.mono, fontSize: type.tMicro, color: colors.textDim, flexShrink: 0},
});
