import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, css, type} from '../theme/tokens.stylex';

/* ── Styles for BrowserPanel.tsx ───────────────────────────────────────
   The same fixed side sheet the artifact panel uses, minus the fixed
   width — this one is dragged, so the width arrives as an inline style
   and everything here has to leave it alone. */

export const browserPanel = stylex.create({
  root: {
    position: 'fixed',
    insetBlock: 0,
    insetInlineEnd: 0,
    // No `width` here on purpose: useResizableWidth owns it.
    maxWidth: {default: '90vw', '@media (max-width: 860px)': '100%'},
    paddingBlockStart: {default: null, '@media (max-width: 860px)': css.safeTop},
    paddingBlockEnd: {default: null, '@media (max-width: 860px)': css.safeBottom},
    backgroundColor: {default: colors.glassBg, '@media (max-width: 860px)': colors.bg},
    backdropFilter: {default: 'blur(14px)', '@media (max-width: 860px)': 'none'},
    WebkitBackdropFilter: {default: 'blur(14px)', '@media (max-width: 860px)': 'none'},
    borderInlineStartWidth: {default: 1, '@media (max-width: 860px)': 0},
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.glassBorder,
    boxShadow: `-4px 0 28px rgba(${channels.shadow}, 0.45)`,
    display: 'flex',
    flexDirection: 'column',
    zIndex: 30,
    animationName: kf.slideIn,
    animationDuration: '180ms',
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
  heading: {display: 'flex', alignItems: 'center', gap: 7, color: colors.textDim},
  title: {fontSize: type.tUi, fontWeight: 600, color: colors.text},
  liveDot: {
    width: 6,
    height: 6,
    borderRadius: '50%',
    backgroundColor: colors.signalLive,
    animationName: kf.pulse,
    animationDuration: '1.6s',
    animationIterationCount: 'infinite',
  },
  urlBar: {
    paddingInline: 16,
    paddingBlock: 6,
    fontSize: type.tSmall,
    color: colors.textDim,
    fontFamily: type.mono,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
    direction: 'rtl',  // keep the path visible when a long URL is clipped
    textAlign: 'left',
  },
  stage: {
    flex: 1,
    minHeight: 0,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 10,
    overflow: 'auto',
    backgroundColor: colors.surface,
  },
  canvas: {
    maxWidth: '100%',
    maxHeight: '100%',
    // The frame's aspect ratio wins; the panel just gives it room.
    objectFit: 'contain',
    borderRadius: 4,
    boxShadow: `0 2px 14px rgba(${channels.shadow}, 0.35)`,
  },
  canvasHidden: {display: 'none'},
  notice: {fontSize: type.tSmall, color: colors.textDim, textAlign: 'center', margin: 0},
  reason: {display: 'block', marginBlockStart: 6, opacity: 0.75},
  footer: {
    paddingInline: 16,
    paddingBlock: 8,
    fontSize: type.tSmall,
    color: colors.textFaint,
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    flexShrink: 0,
  },
});
