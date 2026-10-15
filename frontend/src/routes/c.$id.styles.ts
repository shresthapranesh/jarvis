import * as stylex from '@stylexjs/stylex';

import {colors, css, layout, space, type} from '../theme/tokens.stylex';

/* ── Styles for routes/c.$id.tsx ───────────────────────────────────────
   The page chrome around the thread: the incognito notice and the footer
   the composer sits in. The header, the thread and the run spine own their
   own styling. */

export const conv = stylex.create({
  /** Shown only while the conversation is ephemeral. */
  incognitoBar: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexShrink: 0,
    paddingBlock: 7,
    paddingInline: 16,
    fontSize: type.tSmall,
    color: colors.textDim,
    backgroundColor: `color-mix(in srgb, ${colors.accent} 8%, transparent)`,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
  },

  footer: {
    paddingBlockStart: 10,
    paddingBlockEnd: `calc(14px + ${css.safeBottom})`,
    paddingInline: {default: 20, '@media (max-width: 860px)': 12},
    backgroundColor: 'transparent',
    flexShrink: 0,
  },
  /**
   * The run spine is fixed to the right edge, so the footer reserves its
   * width — the same trick MessageThread uses, and for the same reason: no
   * compiled style can express `.page.has-spine .page-footer`.
   */
  footerWithSpine: {
    paddingInlineEnd: {
      default: `calc(${layout.spineW} + ${space.s5})`,
      '@media (max-width: 1100px)': space.s5,
      '@media (max-width: 860px)': 12,
    },
  },
  /**
   * A settled rail is 36px wide, so the footer reserves that instead — the
   * mirror of `scrollerWithSpineCollapsed` in MessageThread, breakpoints
   * included. Applied after `footerWithSpine` so it wins.
   */
  footerWithSpineCollapsed: {
    paddingInlineEnd: {
      default: `calc(${layout.spineCollapsedW} + ${space.s5})`,
      '@media (max-width: 1100px)': space.s5,
      '@media (max-width: 860px)': 12,
    },
  },
});
