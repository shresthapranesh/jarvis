import * as stylex from '@stylexjs/stylex';

import {colors, radii, type} from '../theme/tokens.stylex';

/* ── Styles for InputBox.tsx ───────────────────────────────────────────
   The composer card and its footer row, plus the strip of attachment
   thumbnails that appears above the textarea once files are added. */

/** The card: textarea, footer row, and the focus treatment across both. */
export const composer = stylex.create({
  // 740, not 760: the thread's `turn.base` is 740 and both sit in containers
  // with the same 20px inline padding, so this is what makes the composer's
  // edges line up with the column of text above it instead of missing it by
  // 10px on each side.
  wrap: {maxWidth: {default: 740, '@media (max-width: 860px)': 'none'}, marginInline: 'auto'},
  wrapFull: {maxWidth: 'none'},
  /**
   * One field, not a card on a page.
   *
   * This carried the whole of the old theme's depth kit — a real
   * `blur(14px)` (hardcoded, so the `layout.blur` token never reached it), a
   * `0 6px 24px` drop shadow, an inset top highlight, and a `translateY(-1px)`
   * lift on focus. On a ground with no light source all four were describing
   * an elevation that isn't there. Focus is now the one thing it can be on
   * paper: the rule around the field gets darker.
   */
  card: {
    backgroundColor: colors.glassBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus-within': colors.borderStrong},
    borderRadius: radii.lg,
    display: 'flex',
    flexDirection: 'column',
    // The keyboard hint is a grandchild and cannot see this element's
    // `:focus-within`, so the state is published as a custom property — the
    // same trick `turn.base` uses for its action row.
    '--composer-hint-opacity': {default: '0', ':focus-within': '1'},
    transition: 'border-color 0.15s ease',
  },
  cardDisabled: {opacity: 0.6},
  cardIncognito: {borderColor: `color-mix(in srgb, ${colors.accent} 45%, ${colors.border})`},
  textarea: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    outline: 'none',
    resize: 'none',
    color: colors.text,
    fontSize: type.tBody,
    fontFamily: 'inherit',
    lineHeight: 1.55,
    letterSpacing: '-0.01em',
    paddingBlock: '12px 7px',
    paddingInline: 14,
    minHeight: 42,
    maxHeight: 160,
    '::placeholder': {color: colors.textDim},
  },
  // No rule above this row. With one it read as a card divided into a text
  // pane and a toolbar; without it the controls simply sit at the foot of a
  // single field, which is the whole of the redesign.
  footer: {
    display: 'flex',
    alignItems: 'center',
    gap: 4,
    paddingBlock: '2px 7px',
    paddingInlineStart: 10,
    paddingInlineEnd: 8,
  },
  // Everything in the footer row must be able to shrink on a phone, or the
  // send button — the one control the composer exists for — is pushed past
  // the right edge once iOS forces 16px controls.
  hint: {
    flex: 1,
    fontSize: type.tMicro,
    color: colors.textFaint,
    textAlign: 'right',
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    paddingInlineEnd: 4,
  },
  /**
   * Keyboard advice fades in with focus — it is instruction for someone
   * already typing, and a resting composer should not be repeating it.
   * Applied only when the hint is NOT carrying live speech text, which has to
   * stay visible whether or not the textarea holds focus.
   */
  hintIdle: {
    opacity: 'var(--composer-hint-opacity)',
    transition: 'opacity 0.18s ease',
  },
});

/**
 * The footer controls — attach, incognito, mic, model, send.
 *
 * There is no `icon` style here any more: attach/incognito/mic now use
 * `iconBtn` from `./ui`, which is the same 26px borderless glyph every other
 * dense row in the app uses. Three controls each drawing their own fill and
 * border turned the footer into a row of little boxes; the app already had
 * one answer for this and the composer was not using it.
 */
export const control = stylex.create({
  /** Rounds `iconBtn.base` to match the field it sits in. */
  glyph: {borderRadius: radii.md},
  /** Toggled-on state for `iconBtn.base` — incognito armed, mic listening. */
  iconActive: {
    color: {default: colors.accent, ':hover': colors.accent},
    backgroundColor: {default: colors.accentDim, ':hover': colors.accentDim},
  },
  model: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    outline: 'none',
    color: {default: colors.textFaint, ':hover': colors.text, ':focus': colors.text},
    fontSize: type.tSmall,
    fontFamily: type.mono,
    cursor: 'pointer',
    transition: 'color 0.15s',
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
    borderRadius: radii.md,
    borderStyle: 'none',
    backgroundColor: {default: colors.accent, ':hover:not(:disabled)': colors.accentStrong},
    color: colors.accentContrast,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.3},
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
    // A press, not a hop. The gradient, the glow and the `scale(1.04)` hover
    // all described a control floating above the page.
    transform: {default: null, ':active:not(:disabled)': 'translateY(1px)'},
    transition: 'background-color 0.15s ease, opacity 0.15s ease',
  },
  sendStop: {
    // Solid on hover rather than the old translucent `errorBorder`, which is
    // what lets `dangerContrast` be the right text colour in both themes —
    // the `#fff` it replaced vanished against a pale wash on light stock.
    backgroundColor: {default: 'transparent', ':hover:not(:disabled)': colors.danger},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.errorBorder,
    color: {default: colors.errorText, ':hover:not(:disabled)': colors.dangerContrast},
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
    borderRadius: 2,
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
    borderRadius: 2,
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
    fontSize: type.tNano,
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
    color: colors.accentContrast,
    fontSize: type.tNano,
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
