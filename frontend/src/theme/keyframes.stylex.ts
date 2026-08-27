import * as stylex from '@stylexjs/stylex';

import {channels, colors} from './tokens.stylex';

/**
 * Every animation in the app.
 *
 * Two shapes are forced here. `stylex.keyframes` is a compile-time call, so it
 * has to be a plain top-level binding — it cannot sit inside an object literal.
 * And a keyframe name is only usable in another module if it is re-exported
 * through `defineVars`, which is what `kf` at the bottom is for; consumers
 * write `animationName: kf.spin`, so a rename is a type error rather than a
 * silently dead animation.
 *
 * Duplicates from styles.css were folded: `slide-in` and `slide-in-right` were
 * identical, as were `spin` and `board-split-spin`, and `confirm-fade` /
 * `nav-scrim-in` are both a plain fade (now `fadeIn`).
 */

const brandPulse = stylex.keyframes({
  '0%, 100%': {boxShadow: `0 0 0 0 rgba(${channels.signalLive}, 0.4)`},
  '50%': {boxShadow: `0 0 0 5px rgba(${channels.signalLive}, 0)`},
});

const msgEnter = stylex.keyframes({
  from: {opacity: 0, transform: 'translateY(10px)'},
  to: {opacity: 1, transform: 'translateY(0)'},
});

const streamFade = stylex.keyframes({
  from: {opacity: 0, transform: 'translateY(2px)'},
  to: {opacity: 1, transform: 'translateY(0)'},
});

const dispatchEnter = stylex.keyframes({
  from: {opacity: 0, transform: 'translateY(8px)'},
  to: {opacity: 1, transform: 'translateY(0)'},
});

const panelEnter = stylex.keyframes({
  from: {opacity: 0, transform: 'translateX(16px)'},
  to: {opacity: 1, transform: 'translateX(0)'},
});

const stepEnter = stylex.keyframes({
  from: {opacity: 0, transform: 'translateX(8px)'},
  to: {opacity: 1, transform: 'translateX(0)'},
});

const slideIn = stylex.keyframes({
  from: {transform: 'translateX(100%)'},
  to: {transform: 'translateX(0)'},
});

const fadeIn = stylex.keyframes({
  from: {opacity: 0},
  to: {opacity: 1},
});

const confirmZoom = stylex.keyframes({
  from: {transform: 'scale(0.94)', opacity: 0},
  to: {transform: 'scale(1)', opacity: 1},
});

const toastIn = stylex.keyframes({
  from: {transform: 'translateY(6px) translateX(12px) scale(0.98)', opacity: 0},
  to: {transform: 'translateY(0) translateX(0) scale(1)', opacity: 1},
});

const spin = stylex.keyframes({
  to: {transform: 'rotate(360deg)'},
});

const bounce = stylex.keyframes({
  '0%, 80%, 100%': {transform: 'translateY(0)', opacity: 0.4},
  '40%': {transform: 'translateY(-6px)', opacity: 1},
});

const blink = stylex.keyframes({
  '0%, 100%': {opacity: 1},
  '50%': {opacity: 0},
});

const cursorBlink = stylex.keyframes({
  '50%': {opacity: 0},
});

const pulse = stylex.keyframes({
  '0%, 100%': {opacity: 1},
  '50%': {opacity: 0.55},
});

const livePulse = stylex.keyframes({
  '0%, 100%': {opacity: 1},
  '50%': {opacity: 0.4},
});

const railPulse = stylex.keyframes({
  '0%, 100%': {opacity: 1},
  '50%': {opacity: 0.45},
});

const workerPulse = stylex.keyframes({
  '0%, 100%': {opacity: 0.35},
  '50%': {opacity: 1},
});

const pulseDot = stylex.keyframes({
  '0%, 100%': {opacity: 1, transform: 'scale(1)'},
  '50%': {opacity: 0.5, transform: 'scale(0.7)'},
});

const orbPulse = stylex.keyframes({
  '0%, 100%': {transform: 'scale(1)', opacity: 1},
  '50%': {transform: 'scale(1.45)', opacity: 0.6},
});

const orbSpinRing = stylex.keyframes({
  '0%': {boxShadow: `2px 0 0 0 ${colors.accent}`, transform: 'rotate(0deg)'},
  '100%': {boxShadow: `2px 0 0 0 ${colors.accent}`, transform: 'rotate(360deg)'},
});

const wfPulse = stylex.keyframes({
  '0%, 100%': {
    boxShadow: `0 0 0 2px ${colors.accent}, 0 2px 8px rgba(${channels.shadow}, 0.3)`,
  },
  '50%': {
    boxShadow: `0 0 0 5px ${colors.accentDim}, 0 2px 8px rgba(${channels.shadow}, 0.3)`,
  },
});

const spinePing = stylex.keyframes({
  '0%, 100%': {
    boxShadow: `0 0 0 3px ${colors.bg}, 0 0 0 4px rgba(${channels.signalLive}, 0.35)`,
  },
  '50%': {
    boxShadow: `0 0 0 3px ${colors.bg}, 0 0 0 8px rgba(${channels.signalLive}, 0)`,
  },
});

const glowSweep = stylex.keyframes({
  '0%': {backgroundPosition: '200% 0'},
  '100%': {backgroundPosition: '-200% 0'},
});

/** The shared surface — see the note above on why the indirection exists. */
export const kf = stylex.defineVars({
  brandPulse,
  msgEnter,
  streamFade,
  dispatchEnter,
  panelEnter,
  stepEnter,
  slideIn,
  fadeIn,
  confirmZoom,
  toastIn,
  spin,
  bounce,
  blink,
  cursorBlink,
  pulse,
  livePulse,
  railPulse,
  workerPulse,
  pulseDot,
  orbPulse,
  orbSpinRing,
  wfPulse,
  spinePing,
  glowSweep,
});
