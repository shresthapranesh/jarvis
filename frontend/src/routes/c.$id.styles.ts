import * as stylex from '@stylexjs/stylex';

import {colors, css, layout, space} from '../theme/tokens.stylex';

/* ── Styles for routes/c.$id.tsx ───────────────────────────────────────
   The page chrome around the thread: the incognito notice, the project
   badge, and the footer the composer sits in. The thread and the run
   spine own their own styling. */

export const conv = stylex.create({
  /** Shown only while the conversation is ephemeral. */
  incognitoBar: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexShrink: 0,
    paddingBlock: 7,
    paddingInline: 16,
    fontSize: '0.72rem',
    color: colors.textDim,
    backgroundColor: `color-mix(in srgb, ${colors.accent} 8%, transparent)`,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
  },

  projectBar: {display: 'flex', paddingBlock: '8px 0', paddingInline: 16, flexShrink: 0},
  projectBadge: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    fontSize: '0.72rem',
    color: {default: colors.textDim, ':hover': colors.text},
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.borderStrong},
    borderRadius: 999,
    paddingBlock: 3,
    paddingInline: 10,
    textDecoration: 'none',
    transition: 'color 0.15s ease, border-color 0.15s ease',
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
});
