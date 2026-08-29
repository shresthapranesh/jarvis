import * as stylex from '@stylexjs/stylex';

import {channels, colors, type} from '../theme/tokens.stylex';

/* ── Styles for MessageBubble.tsx ──────────────────────────────────────
   One group per thing a turn can be: the user's bubble, the "still
   working" placeholder, the action row under a finished turn, its
   badges, and the attachment chips a multimodal message carries. */

/**
 * The user's own message — the only side of the exchange that gets a bubble.
 * Its position is what attributes it, so it needs no label.
 *
 * A block of stock with a rule down its leading edge, not a floating bubble:
 * nothing in this theme casts a shadow or lifts on hover, because nothing in
 * it sits above the page.
 */
export const bubble = stylex.create({
  user: {
    alignSelf: 'flex-end',
    backgroundColor: colors.userBg,
    borderInlineStartWidth: 2,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.borderStrong,
    paddingBlock: 10,
    paddingInline: 14,
    maxWidth: '71%',
    lineHeight: 1.6,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: type.tBody,
    transition: 'border-color 0.15s',
  },
});

/** The placeholder a turn shows before any text has streamed. */
export const working = stylex.create({
  root: {display: 'flex', flexDirection: 'column', gap: 5, paddingBlock: 10, paddingInline: 2},
  action: {display: 'flex', alignItems: 'center', gap: 8},
  label: {fontSize: type.tUi, color: colors.textDim},
  preview: {
    fontSize: type.tSmall,
    color: colors.textFaint,
    fontFamily: type.mono,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    maxWidth: 520,
    paddingInlineStart: 30,
    lineHeight: 1.4,
  },
  thinking: {
    fontSize: type.tSmall,
    color: colors.textFaint,
    fontStyle: 'italic',
    fontFamily: type.mono,
    lineHeight: 1.5,
    maxHeight: 96,
    overflowY: 'auto',
    paddingInlineStart: 30,
    paddingInlineEnd: 8,
    scrollbarWidth: 'none',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    '::-webkit-scrollbar': {display: 'none'},
  },
});

/** The recessive row of controls under a settled turn. */
export const actions = stylex.create({
  // Resting turns used to show nothing at all until hovered, which made settled
  // conversations look inert. Keep the row present but recessive; `turn.base`
  // publishes the hover value.
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    opacity: {default: 'var(--turn-actions-opacity)', ':focus-within': 1},
    transition: 'opacity 0.15s',
  },
  rowUser: {justifyContent: 'flex-end'},
  // Resting turns used to show nothing at all until hovered, which made settled
  // conversations look inert. Keep the row present but recessive; `turn.base`
  // publishes the hover value.
  /** Held visible while text-to-speech is loading or playing. */
  rowPinned: {opacity: 1},
  copy: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 24,
    height: 24,
    backgroundColor: {
      default: `rgba(${channels.tint}, 0.04)`,
      ':hover': `rgba(${channels.accent}, 0.06)`,
    },
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {
      default: `rgba(${channels.tint}, 0.09)`,
      ':hover': `rgba(${channels.accent}, 0.3)`,
    },
    borderRadius: 2,
    color: {default: colors.textDim, ':hover': colors.accent},
    cursor: 'pointer',
    padding: 0,
    transition: 'color 0.15s, border-color 0.15s, background 0.15s',
  },
  copyDone: {color: colors.ok, borderColor: `rgba(${channels.ok}, 0.3)`},
});

/** Token counts and throughput for the turn. */
export const badge = stylex.create({
  base: {
    color: colors.textDim,
    fontSize: type.tMicro,
    whiteSpace: 'nowrap',
    paddingBlock: 3,
    paddingInline: 4,
    cursor: 'default',
  },
  /* pp = prompt processing (prefill), tg = text generation (eval) — the
     llama.cpp shorthand, spelled out in the badge's title. */
  perf: {display: 'inline-flex', gap: 6, fontVariantNumeric: 'tabular-nums'},
});

/** Historical rows the removed safety gates persisted as blocked. */
export const safety = stylex.create({
  banner: {
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'baseline',
    gap: 8,
    backgroundColor: colors.warningBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.warningBorder,
    borderRadius: 3,
    paddingBlock: 8,
    paddingInline: 12,
    marginBlockEnd: 6,
    color: colors.warningText,
    fontSize: type.tUi,
    lineHeight: 1.35,
  },
  label: {fontWeight: 600, whiteSpace: 'nowrap'},
});

/** Attachment chips inside a multimodal user message. */
export const media = stylex.create({
  stack: {display: 'flex', flexDirection: 'column', gap: 6},
  text: {margin: 0, whiteSpace: 'pre-wrap', wordBreak: 'break-word'},
  chip: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    backgroundColor: `rgba(${channels.tint}, 0.07)`,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: `rgba(${channels.tint}, 0.12)`,
    borderRadius: 2,
    paddingBlock: 4,
    paddingInline: 9,
    fontSize: type.tUi,
    color: colors.text,
    maxWidth: '100%',
  },
  size: {color: `rgba(${channels.tint}, 0.45)`, fontSize: type.tMicro, marginInlineStart: 2},
});
