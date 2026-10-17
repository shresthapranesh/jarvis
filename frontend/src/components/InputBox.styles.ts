import * as stylex from '@stylexjs/stylex';

import {bp, colors, radii, type} from '../theme/tokens.stylex';

/* ── Styles for InputBox.tsx ───────────────────────────────────────────
   The composer card and its footer row. */

/** The card: textarea, footer row, and the focus treatment across both. */
export const composer = stylex.create({
  // 740, not 760: the thread's `turn.base` is 740 and both sit in containers
  // with the same 20px inline padding, so this is what makes the composer's
  // edges line up with the column of text above it instead of missing it by
  // 10px on each side.
  wrap: {maxWidth: {default: 740, '@media (max-width: 860px)': 'none'}, marginInline: 'auto'},
  wrapFull: {maxWidth: 'none'},
  /**
   * One field, not a card on a page: a step of fill and no rule. Focus lifts
   * the fill a step rather than drawing an outline. The 1px stays, clear, so
   * the queueing and incognito states below can draw theirs without the
   * field shifting.
   */
  card: {
    backgroundColor: {default: colors.surface2, ':focus-within': colors.surface3},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: 'transparent',
    borderRadius: radii.lg,
    display: 'flex',
    flexDirection: 'column',
    transition: 'background-color 0.15s ease, border-color 0.15s ease',
  },
  cardDisabled: {opacity: 0.6},
  /**
   * Queueing: the field is live but what it does has changed, so it says so
   * with a rule rather than by dimming — dimming is what `cardDisabled` means
   * here, and this is the opposite state.
   */
  cardQueueing: {
    borderStyle: 'dashed',
    borderColor: `color-mix(in srgb, ${colors.accent} 35%, ${colors.border})`,
  },
  cardIncognito: {borderColor: `color-mix(in srgb, ${colors.accent} 45%, ${colors.border})`},
  /** A file held over the field: it will take it. */
  cardDragging: {
    backgroundColor: colors.surface3,
    borderStyle: 'dashed',
    borderColor: colors.borderStrong,
  },
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
    overflowY: 'hidden',
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
  /** Pushes stop/send to the right edge; it gives way first on a phone. */
  spacer: {flex: 1, minWidth: 0},
});

/**
 * The files attached to the draft, above the text: an image as its thumbnail,
 * anything else as its name and size. Fills, no frames, like the rest of the
 * field.
 */
export const attachment = stylex.create({
  strip: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 6,
    paddingBlockStart: 10,
    paddingInline: 10,
  },
  chip: {
    position: 'relative',
    height: 52,
    borderRadius: radii.md,
    backgroundColor: colors.surface3,
    overflow: 'hidden',
    flexShrink: 0,
    transition: 'opacity 0.15s ease',
    // The remove button is a child and cannot see this element's hover.
    '--chip-remove-opacity': {default: '0', ':hover': '1', ':focus-within': '1', [bp.coarse]: '1'},
  },
  chipImage: {width: 52},
  chipFile: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    maxWidth: 220,
    paddingInlineStart: 10,
    paddingInlineEnd: 26,
  },
  uploading: {opacity: 0.55},
  failed: {backgroundColor: colors.errorBg, color: colors.errorText},
  thumb: {width: '100%', height: '100%', objectFit: 'cover', display: 'block'},
  icon: {flexShrink: 0, color: colors.textDim},
  meta: {display: 'flex', flexDirection: 'column', gap: 2, minWidth: 0},
  name: {
    fontSize: type.tSmall,
    color: colors.text,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  size: {fontSize: type.tMicro, color: colors.textFaint},
  remove: {
    position: 'absolute',
    insetBlockStart: 4,
    insetInlineEnd: 4,
    width: 18,
    height: 18,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    padding: 0,
    borderStyle: 'none',
    borderRadius: '50%',
    backgroundColor: {default: 'rgba(0, 0, 0, 0.55)', ':hover': 'rgba(0, 0, 0, 0.8)'},
    color: '#fff',
    cursor: 'pointer',
    opacity: 'var(--chip-remove-opacity)',
    transition: 'opacity 0.15s ease, background-color 0.15s ease',
  },
});

/**
 * The footer controls — incognito and send; the model picker styles itself
 * (`ModelPicker.tsx`).
 *
 * There is no `icon` style here any more: incognito now uses
 * `iconBtn` from `./ui`, which is the same 26px borderless glyph every other
 * dense row in the app uses. Three controls each drawing their own fill and
 * border turned the footer into a row of little boxes; the app already had
 * one answer for this and the composer was not using it.
 */
export const control = stylex.create({
  /** Rounds `iconBtn.base` to match the field it sits in. */
  glyph: {borderRadius: radii.md},
  /** Toggled-on state for `iconBtn.base` — incognito armed. */
  iconActive: {
    color: {default: colors.accent, ':hover': colors.accent},
    backgroundColor: {default: colors.accentDim, ':hover': colors.accentDim},
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
  /**
   * Nothing typed yet: the arrow waits as a glyph rather than a filled block,
   * and fills in once there is something to send.
   */
  sendIdle: {
    backgroundColor: {default: 'transparent', ':hover:not(:disabled)': colors.surface3},
    color: colors.textFaint,
  },
  sendStop: {
    // Solid on hover, which is what lets `dangerContrast` be the right text
    // colour in both themes.
    backgroundColor: {default: colors.surface3, ':hover:not(:disabled)': colors.danger},
    color: {default: colors.text, ':hover:not(:disabled)': colors.dangerContrast},
  },
});
