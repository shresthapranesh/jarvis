import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {bp, channels, colors, css, layout, radii, space, type} from '../theme/tokens.stylex';

/* ════════════════════════════════════════════════════════════════════
   Styles for the app shell (routes/__root.tsx).

   Split into one `stylex.create` per part of the frame rather than a
   single object: the groups are what the shell is actually made of, and
   each is small enough to read. StyleX composes them at the call site —
   `stylex.props(rail.root, collapsed && rail.collapsed)` — so nothing is
   lost by the split.
   ════════════════════════════════════════════════════════════════════ */

/** The three-column frame: rail, main column, and the host the route mounts into. */
export const shell = stylex.create({
  appShell: {
    display: 'flex',
    height: `calc(100dvh - ${css.kbInset})`,
    overflow: 'hidden',
    viewTransitionName: 'root',
  },
  mainPanel: {
    flex: 1,
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
    minWidth: 0,
    viewTransitionName: 'main',
  },
  outletHost: {display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0},
});

/** The left panel — a resizable rail on a desktop, an off-canvas drawer below 860px. */
export const rail = stylex.create({
  root: {
    position: {default: 'relative', [bp.compact]: 'fixed'},
    insetBlock: {default: null, [bp.compact]: 0},
    insetInlineStart: {default: null, [bp.compact]: 0},
    zIndex: {default: null, [bp.compact]: 110},
    // The drawer width is fixed; on desktop an inline style supplies it.
    width: {default: layout.leftW, [bp.compact]: 'min(300px, 84vw)'},
    flexShrink: 0,
    // Opaque over the scrim: the glass treatment is for a rail sitting beside
    // content, not a drawer sitting on top of it.
    backgroundColor: {default: colors.glassBg, [bp.compact]: colors.bg},
    backdropFilter: {default: layout.blur, [bp.compact]: 'none'},
    WebkitBackdropFilter: {default: layout.blur, [bp.compact]: 'none'},
    borderInlineEndWidth: 1,
    borderInlineEndStyle: 'solid',
    borderInlineEndColor: colors.glassBorder,
    boxShadow: `4px 0 20px rgba(${channels.shadow}, 0.28), inset -1px 0 0 rgba(${channels.tint}, 0.04)`,
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
    paddingInlineStart: {default: null, [bp.compact]: css.safeLeft},
    paddingBlockEnd: {default: null, [bp.compact]: css.safeBottom},
    transform: {default: null, [bp.compact]: 'translateX(-100%)'},
    transition: {
      default: 'width 0.22s cubic-bezier(0.2, 0.8, 0.2, 1)',
      [bp.compact]: 'transform 0.24s cubic-bezier(0.2, 0.8, 0.2, 1)',
    },
    // A fixed, translated element makes a poor view-transition subject, and the
    // drawer already animates itself.
    viewTransitionName: {default: 'nav', [bp.compact]: 'none'},
  },
  // The drawer width is fixed; on desktop an inline style supplies it.
  // Opaque over the scrim: the glass treatment is for a rail sitting beside
  // content, not a drawer sitting on top of it.
  // A fixed, translated element makes a poor view-transition subject, and the
  // drawer already animates itself.
  collapsed: {width: layout.leftCollapsedW},
  // The drawer width is fixed; on desktop an inline style supplies it.
  // Opaque over the scrim: the glass treatment is for a rail sitting beside
  // content, not a drawer sitting on top of it.
  // A fixed, translated element makes a poor view-transition subject, and the
  // drawer already animates itself.
  resizing: {transition: 'none'},
  // The drawer width is fixed; on desktop an inline style supplies it.
  // Opaque over the scrim: the glass treatment is for a rail sitting beside
  // content, not a drawer sitting on top of it.
  // A fixed, translated element makes a poor view-transition subject, and the
  // drawer already animates itself.
  open: {transform: {default: null, [bp.compact]: 'translateX(0)'}},
  handle: {
    position: 'absolute',
    insetBlockStart: 0,
    insetInlineEnd: 0,
    width: 5,
    height: '100%',
    cursor: 'col-resize',
    zIndex: 5,
    transition: 'background 0.15s',
    backgroundColor: {default: 'transparent', ':hover': colors.accentDim},
    // A 5px drag target means nothing on touch.
    display: {default: 'block', [bp.compact]: 'none'},
  },
  // A 5px drag target means nothing on touch.
  handleActive: {backgroundColor: colors.accentDim},
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: space.s2,
    paddingBlock: `${space.s4} ${space.s3}`,
    paddingInline: space.s3,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  headerCollapsed: {justifyContent: 'center', gap: 0},
});

/** Product mark, wordmark, and the one line that always answers "is it doing anything?". */
export const brand = stylex.create({
  mark: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    color: colors.accent,
    flexShrink: 0,
  },
  block: {display: 'flex', flexDirection: 'column', gap: 1, flex: 1, minWidth: 0},
  name: {
    fontFamily: type.display,
    fontSize: '0.9375rem',
    fontWeight: 650,
    letterSpacing: '-0.025em',
    color: colors.text,
    lineHeight: 1.1,
  },
  /* The one line in the shell that always answers "is it doing anything?" */
  status: {
    display: 'flex',
    alignItems: 'center',
    gap: 5,
    fontFamily: type.mono,
    fontSize: type.tMicro,
    letterSpacing: '0.02em',
    color: colors.textFaint,
    lineHeight: 1.2,
  },
  /* The one line in the shell that always answers "is it doing anything?" */
  statusBusy: {color: colors.signalLive},
  dot: {
    width: 6,
    height: 6,
    borderRadius: '50%',
    backgroundColor: colors.textFaint,
    flexShrink: 0,
    transition: 'background 0.4s',
    boxShadow: '0 0 0 4px transparent',
  },
  dotOk: {backgroundColor: colors.ok, boxShadow: `0 0 0 4px rgba(${channels.ok}, 0.14)`},
  dotErr: {
    backgroundColor: colors.danger,
    boxShadow: `0 0 0 4px rgba(${channels.danger}, 0.14)`,
  },
  dotPulsing: {
    animationName: kf.brandPulse,
    animationDuration: '1.6s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
});

/** The small ghost buttons in the rail header, and the icons they animate. */
export const control = stylex.create({
  ghost: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: {default: 24, [bp.compact]: 40},
    height: {default: 24, [bp.compact]: 40},
    borderRadius: 7,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: 'transparent', ':hover': colors.accentDim},
    backgroundColor: {default: 'transparent', ':hover': `rgba(${channels.tint}, 0.05)`},
    color: {default: colors.textFaint, ':hover': colors.accent},
    cursor: 'pointer',
    flexShrink: 0,
    transition: 'color 0.15s, border-color 0.15s, background 0.15s',
    padding: 0,
  },
  // The icon's rotation is driven from the *button's* hover, which no selector
  // on the icon itself can see — so the button publishes it as a custom
  // property and the icon reads it. Same trick as `navIcon` below.
  themeToggle: {'--theme-icon-rot': {default: '0deg', ':hover': '18deg'}},
  // The icon's rotation is driven from the *button's* hover, which no selector
  // on the icon itself can see — so the button publishes it as a custom
  // property and the icon reads it. Same trick as `navIcon` below.
  themeIcon: {
    transform: 'rotate(var(--theme-icon-rot))',
    transition: 'transform 0.4s cubic-bezier(0.4, 0, 0.2, 1)',
  },
  // The icon's rotation is driven from the *button's* hover, which no selector
  // on the icon itself can see — so the button publishes it as a custom
  // property and the icon reads it. Same trick as `navIcon` below.
  chevron: {transition: 'transform 0.22s ease'},
  // The icon's rotation is driven from the *button's* hover, which no selector
  // on the icon itself can see — so the button publishes it as a custom
  // property and the icon reads it. Same trick as `navIcon` below.
  chevronFlipped: {transform: 'rotate(180deg)'},
});

/** The grouped navigation rows and their live-count badges. */
export const nav = stylex.create({
  list: {
    paddingBlock: `${space.s3} ${space.s2}`,
    paddingInline: space.s2,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: space.s4,
  },
  listCollapsed: {paddingBlock: 6, paddingInline: 0, alignItems: 'center'},
  group: {display: 'flex', flexDirection: 'column', gap: 1},
  /* Eyebrow — the device that turns 11 equal rows into 3 readable intents. */
  heading: {
    fontSize: type.tMicro,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: type.trackMicro,
    color: colors.textFaint,
    paddingBlock: `0 ${space.s2}`,
    paddingInline: 10,
  },
  link: {
    position: 'relative',
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    paddingBlock: {default: 7, [bp.compact]: 11},
    paddingInline: 10,
    fontSize: type.tUi,
    fontWeight: 450,
    letterSpacing: '-0.01em',
    color: {default: colors.textDim, ':hover': colors.text},
    textDecoration: 'none',
    borderRadius: radii.sm,
    backgroundColor: {default: 'transparent', ':hover': `rgba(${channels.tint}, 0.05)`},
    transition:
      'background 0.18s cubic-bezier(0.2, 0.8, 0.2, 1), color 0.18s cubic-bezier(0.2, 0.8, 0.2, 1)',
    '--nav-icon-opacity': {default: '0.75', ':hover': '1'},
  },
  icon: {flexShrink: 0, opacity: 'var(--nav-icon-opacity)', transition: 'opacity 0.18s'},
  linkActive: {
    backgroundColor: colors.accentDim,
    color: colors.accent,
    fontWeight: 550,
    '--nav-icon-opacity': '1',
  },
  /* Signal bar rather than a fill alone — reads as "current channel". */
  linkActiveBar: {
    '::before': {
      content: '',
      position: 'absolute',
      insetInlineStart: 0,
      insetBlockStart: '50%',
      transform: 'translateY(-50%)',
      width: 2,
      height: 15,
      borderRadius: '0 2px 2px 0',
      backgroundColor: colors.accent,
    },
  },
  /* Signal bar rather than a fill alone — reads as "current channel". */
  linkCollapsed: {justifyContent: 'center', paddingBlock: 9, paddingInline: 0, width: '100%'},
  badge: {
    marginInlineStart: 'auto',
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    minWidth: 20,
    height: 18,
    paddingBlock: 0,
    paddingInline: 6,
    fontSize: '0.7rem',
    fontWeight: 600,
    backgroundColor: colors.accent,
    color: colors.accentContrast,
    borderRadius: 999,
    flexShrink: 0,
  },
  badgeCompact: {
    position: 'absolute',
    insetBlockStart: 4,
    insetInlineEnd: 4,
    marginInlineStart: 0,
    minWidth: 14,
    height: 14,
    fontSize: '0.6rem',
    paddingInline: 3,
  },
});

/** Chrome that only exists below the breakpoint: the drawer scrim and the top bar. */
export const mobile = stylex.create({
  scrim: {
    position: 'fixed',
    inset: 0,
    zIndex: 105,
    borderStyle: 'none',
    padding: 0,
    backgroundColor: `rgba(${channels.shadow}, 0.55)`,
    animationName: kf.fadeIn,
    animationDuration: '0.2s',
    animationTimingFunction: 'ease',
    // Rendered only under the breakpoint anyway; belt and braces if it isn't.
    display: {default: 'none', [bp.compact]: 'block'},
  },
  /* The mobile chrome is always in the DOM; on desktop it is inert. */
  topbar: {
    display: {default: 'none', [bp.compact]: 'flex'},
    alignItems: 'center',
    gap: space.s2,
    flexShrink: 0,
    paddingBlockStart: `calc(${css.safeTop} + 6px)`,
    paddingBlockEnd: 6,
    paddingInlineStart: `calc(${space.s2} + ${css.safeLeft})`,
    paddingInlineEnd: `calc(${space.s2} + ${css.safeRight})`,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    // In normal flow, so nothing ever scrolls under it — a backdrop-filter here
    // would cost a frame's work to blur the flat page background.
    backgroundColor: colors.glassBg,
  },
  /* The mobile chrome is always in the DOM; on desktop it is inert. */
  // In normal flow, so nothing ever scrolls under it — a backdrop-filter here
  // would cost a frame's work to blur the flat page background.
  topbarBrand: {display: 'flex', alignItems: 'baseline', gap: space.s2, flex: 1, minWidth: 0},
  /* The mobile chrome is always in the DOM; on desktop it is inert. */
  // In normal flow, so nothing ever scrolls under it — a backdrop-filter here
  // would cost a frame's work to blur the flat page background.
  topbarName: {fontSize: '0.9rem', fontWeight: 600, letterSpacing: '-0.01em'},
});
