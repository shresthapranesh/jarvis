import * as stylex from '@stylexjs/stylex';

import {channels, colors, radii, type} from '../theme/tokens.stylex';

/* ── Styles for InputBox.tsx ───────────────────────────────────────────
   The composer card and its footer row, plus the strip of attachment
   thumbnails that appears above the textarea once files are added. */

/** The card: textarea, footer row, and the focus treatment across both. */
export const composer = stylex.create({
  wrap: {maxWidth: {default: 760, '@media (max-width: 860px)': 'none'}, marginInline: 'auto'},
  wrapFull: {maxWidth: 'none'},
  card: {
    backgroundColor: colors.glassBg,
    backdropFilter: 'blur(14px)',
    WebkitBackdropFilter: 'blur(14px)',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {
      default: `rgba(${channels.tint}, 0.09)`,
      ':focus-within': `rgba(${channels.accent}, 0.32)`,
    },
    borderRadius: radii.lg,
    display: 'flex',
    flexDirection: 'column',
    boxShadow: {
      default: `0 6px 24px rgba(${channels.shadow}, 0.28), inset 0 1px 0 rgba(${channels.tint}, 0.05)`,
      ':focus-within': `0 0 0 1px rgba(${channels.accent}, 0.14), 0 8px 26px rgba(${channels.shadow}, 0.32)`,
    },
    transform: {default: null, ':focus-within': 'translateY(-1px)'},
    transition:
      'border-color 0.2s cubic-bezier(0.2, 0.8, 0.2, 1), box-shadow 0.2s cubic-bezier(0.2, 0.8, 0.2, 1), transform 0.2s cubic-bezier(0.2, 0.8, 0.2, 1)',
  },
  cardDisabled: {opacity: 0.6},
  cardIncognito: {borderColor: `color-mix(in srgb, ${colors.accent} 45%, ${colors.border})`},
  textarea: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    outline: 'none',
    resize: 'none',
    color: colors.text,
    fontSize: '0.9375rem',
    fontFamily: 'inherit',
    lineHeight: 1.55,
    letterSpacing: '-0.01em',
    paddingBlock: '12px 7px',
    paddingInline: 14,
    minHeight: 42,
    maxHeight: 160,
    '::placeholder': {color: colors.textDim},
  },
  footer: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: '6px 8px',
    paddingInlineStart: 14,
    paddingInlineEnd: 10,
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
  },
  // Everything in the footer row must be able to shrink on a phone, or the
  // send button — the one control the composer exists for — is pushed past
  // the right edge once iOS forces 16px controls.
  hint: {
    flex: 1,
    fontSize: '0.68rem',
    color: colors.textDim,
    textAlign: 'right',
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
});

/** The footer controls — attach, incognito, mic, model, send. */
export const control = stylex.create({
  icon: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 26,
    height: 26,
    backgroundColor: {
      default: `rgba(${channels.tint}, 0.06)`,
      ':hover:not(:disabled)': `rgba(${channels.accent}, 0.08)`,
    },
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {
      default: `rgba(${channels.tint}, 0.12)`,
      ':hover:not(:disabled)': `rgba(${channels.accent}, 0.35)`,
    },
    borderRadius: 6,
    color: {default: colors.textDim, ':hover:not(:disabled)': colors.accent},
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.35},
    flexShrink: 0,
    transform: {default: null, ':active:not(:disabled)': 'scale(0.88)'},
    transition: 'color 0.15s, border-color 0.15s, background 0.15s, transform 0.1s',
  },
  iconActive: {
    color: colors.accent,
    borderColor: `rgba(${channels.accent}, 0.45)`,
    backgroundColor: `rgba(${channels.accent}, 0.12)`,
  },
  model: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    outline: 'none',
    color: {default: colors.textDim, ':focus': colors.text},
    fontSize: '0.72rem',
    fontFamily: type.mono,
    maxWidth: {default: 220, '@media (max-width: 860px)': 120},
    // Everything in the footer row must be able to shrink on a phone, or the
    // send button — the one control the composer exists for — is pushed past
    // the right edge once iOS forces 16px controls.
    flexShrink: {default: 0, '@media (max-width: 860px)': 1},
    minWidth: 0,
  },
  send: {
    width: {default: 28, '@media (max-width: 860px)': 44},
    height: {default: 28, '@media (max-width: 860px)': 44},
    borderRadius: radii.sm,
    borderStyle: 'none',
    backgroundImage: `linear-gradient(135deg, ${colors.accentStrong}, ${colors.accent})`,
    color: colors.accentContrast,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.35},
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
    boxShadow: {
      default: `0 2px 10px rgba(${channels.accent}, 0.3)`,
      ':hover:not(:disabled)': `0 4px 20px rgba(${channels.accent}, 0.55)`,
      ':active:not(:disabled)': `0 1px 6px rgba(${channels.accent}, 0.28)`,
    },
    transform: {
      default: null,
      ':hover:not(:disabled)': 'translateY(-1px) scale(1.04)',
      ':active:not(:disabled)': 'scale(0.94)',
    },
    transition:
      'box-shadow 0.2s cubic-bezier(0.2, 0.8, 0.2, 1), transform 0.15s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.15s',
  },
  sendStop: {
    backgroundImage: 'none',
    backgroundColor: {default: 'transparent', ':hover:not(:disabled)': colors.errorBorder},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.errorBorder,
    color: {default: colors.errorText, ':hover:not(:disabled)': '#fff'},
    boxShadow: 'none',
  },
});

/** Pending and already-saved files, shown above the textarea. */
export const attachment = stylex.create({
  strip: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 8,
    paddingBlock: '10px 4px',
    paddingInline: 14,
  },
  thumb: {position: 'relative', width: 48, height: 48, flexShrink: 0},
  image: {
    width: '100%',
    height: '100%',
    objectFit: 'cover',
    borderRadius: 7,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    display: 'block',
  },
  icon: {
    width: '100%',
    height: '100%',
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 7,
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 3,
    color: colors.textDim,
    overflow: 'hidden',
    padding: 4,
  },
  /** Already persisted to the conversation, rather than pending upload. */
  iconSaved: {
    backgroundColor: colors.accentDim,
    borderColor: colors.accent,
    color: colors.accent,
  },
  name: {
    fontSize: '0.55rem',
    color: colors.textDim,
    textOverflow: 'ellipsis',
    overflow: 'hidden',
    whiteSpace: 'nowrap',
    width: '100%',
    textAlign: 'center',
  },
  remove: {
    position: 'absolute',
    insetBlockStart: -5,
    insetInlineEnd: -5,
    width: 16,
    height: 16,
    backgroundColor: colors.accent,
    borderStyle: 'none',
    borderRadius: '50%',
    color: '#fff',
    fontSize: '0.55rem',
    lineHeight: 1,
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 0,
    opacity: {default: 1, ':hover': 0.8},
    transition: 'opacity 0.15s',
  },
});
