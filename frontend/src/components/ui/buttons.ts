/* ════════════════════════════════════════════════════════════════════
   Buttons — the four shapes the app actually uses.
   ════════════════════════════════════════════════════════════════════ */
import * as stylex from '@stylexjs/stylex';

import {channels, colors, type} from '../../theme/tokens.stylex';

/**
 * The workhorse button. Icons inside it dim to 0.65 and come up on hover,
 * which the button drives through `--btn-icon-opacity` — a child cannot see
 * its parent's `:hover` on its own. Pass `btn.icon` to any svg inside.
 */
export const btn = stylex.create({
  base: {
    fontSize: type.tUi,
    fontWeight: 500,
    lineHeight: 1,
    paddingBlock: 7,
    paddingInline: 11,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': `rgba(${channels.tint}, 0.2)`},
    borderRadius: 3,
    color: colors.text,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    textDecoration: 'none',
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    opacity: {default: 1, ':disabled': 0.55},
    transform: {default: null, ':active': 'translateY(1px)', ':disabled': 'none'},
    transition:
      'background 0.14s ease, border-color 0.14s ease, color 0.14s ease, transform 0.06s ease',
    '--btn-icon-opacity': {default: '0.65', ':hover': '1'},
  },
  primary: {
    backgroundColor: colors.accent,
    color: colors.accentContrast,
    borderColor: colors.accent,
    fontWeight: 600,
    filter: {default: null, ':hover': 'brightness(1.08)'},
    '--btn-icon-opacity': '1',
  },
  success: {
    color: colors.ok,
    backgroundColor: `rgba(${channels.ok}, 0.1)`,
    borderColor: `rgba(${channels.ok}, 0.45)`,
    '--btn-icon-opacity': '1',
  },
  danger: {
    color: colors.errorText,
    backgroundColor: {default: colors.surface, ':hover': colors.errorBg},
    borderColor: {default: colors.border, ':hover': colors.errorBorder},
  },
  small: {fontSize: type.tMicro, paddingBlock: 3, paddingInline: 8, borderRadius: 2},
  /** For an svg inside `btn.base`. */
  icon: {opacity: 'var(--btn-icon-opacity)', flexShrink: 0, transition: 'opacity 0.14s ease'},
});

/** Square, borderless-until-hover button for a lone glyph in a dense row. */
export const iconBtn = stylex.create({
  base: {
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 26,
    height: 26,
    borderRadius: 2,
    backgroundColor: {default: 'transparent', ':hover': colors.surface3},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: 'transparent', ':hover': colors.border},
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.4},
    transform: {default: null, ':active': 'translateY(1px)'},
    transition: 'background 0.12s ease, color 0.12s ease, border-color 0.12s ease',
  },
  danger: {
    backgroundColor: {default: 'transparent', ':hover': colors.errorBg},
    borderColor: {default: 'transparent', ':hover': colors.errorBorder},
    color: {default: colors.textDim, ':hover': colors.errorText},
  },
});

/** A small labelled pill that opens a panel — the chat turn's "steps" control. */
export const chipBtn = stylex.create({
  base: {
    alignSelf: 'flex-start',
    display: 'flex',
    alignItems: 'center',
    gap: 5,
    backgroundColor: 'transparent',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.accentDim},
    borderRadius: 2,
    color: {default: colors.textDim, ':hover': colors.accent},
    fontSize: type.tSmall,
    paddingBlock: 3,
    paddingInline: 9,
    cursor: 'pointer',
    transition: 'color 0.15s, border-color 0.15s',
    fontFamily: 'inherit',
  },
});

/** The 24px close/dismiss affordance on side panels. */
export const closeBtn = stylex.create({
  base: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 24,
    height: 24,
    backgroundColor: {
      default: `rgba(${channels.tint}, 0.04)`,
      ':hover': `rgba(${channels.tint}, 0.08)`,
    },
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {
      default: `rgba(${channels.tint}, 0.09)`,
      ':hover': `rgba(${channels.tint}, 0.15)`,
    },
    borderRadius: 2,
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
    padding: 0,
    transition: 'color 0.15s, border-color 0.15s, background 0.15s',
  },
});

// Repeated rather than spread from a shared const: `stylex.create` is compiled
// away at build time, so every value in it has to be a literal the plugin can
// see — a spread of an outside object is not.
