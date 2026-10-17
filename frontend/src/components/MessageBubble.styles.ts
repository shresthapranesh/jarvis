import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, radii, type} from '../theme/tokens.stylex';

/* ── Styles for MessageBubble.tsx ──────────────────────────────────────
   One group per thing a turn can be: the user's bubble, the "still
   working" placeholder, the action row under a finished turn, its
   badges, and the attachment chips a multimodal message carries. */

/**
 * The user's own message — the only side of the exchange that gets a bubble.
 * Its position is what attributes it, so it needs no label.
 *
 * A block of stock with no rule and no shadow: nothing in this theme sits
 * above the page. It takes the composer's `md` corner rather than the page's
 * square one, because it *is* what the composer sent — the one soft surface
 * the reader typed into, carried up into the thread.
 */
export const bubble = stylex.create({
  /** The bubble and its copy button, side by side, so the button costs no line. */
  row: {
    display: 'flex',
    alignItems: 'flex-end',
    justifyContent: 'flex-end',
    gap: 6,
  },
  user: {
    backgroundColor: colors.userBg,
    borderRadius: radii.md,
    paddingBlock: 9,
    paddingInline: 14,
    maxWidth: '71%',
    minWidth: 0,
    lineHeight: 1.6,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: type.tBody,
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
    gap: 2,
    // Pulls the borderless glyphs back so their icons, not their hit areas,
    // line up with the prose edge above.
    marginInlineStart: -6,
    opacity: {default: 'var(--turn-actions-opacity)', ':focus-within': 1},
    transition: 'opacity 0.15s',
  },
  /**
   * The user's copy button, beside the bubble rather than under it. Hidden
   * until the turn is hovered — the bubble is short, and a standing glyph
   * beside every prompt reads as clutter rather than as an affordance.
   */
  userCopy: {
    opacity: {default: 'var(--turn-aside-opacity)', ':focus-visible': 1},
    transition: 'opacity 0.15s',
  },
  /**
   * Borderless glyphs, like every other dense row in the app: three outlined
   * boxes under every turn turned the thread into a grid of little frames.
   */
  copy: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    flexShrink: 0,
    width: 26,
    height: 26,
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    borderStyle: 'none',
    borderRadius: radii.sm,
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
    padding: 0,
    transition: 'color 0.15s, background-color 0.15s',
  },
  copyDone: {color: {default: colors.ok, ':hover': colors.ok}},
  /** Held lit while the button's popover is open. */
  copyActive: {
    color: colors.text,
    backgroundColor: colors.surface2,
  },
  /**
   * `chipBtn` with its frame taken off, to sit in the same row as the glyphs.
   * It is the way into the work behind the reply, so unlike the glyphs it
   * rests on a fill, in the UI size and full ink, rather than vanishing.
   */
  steps: {
    alignSelf: 'center',
    height: 26,
    gap: 6,
    borderColor: {default: 'transparent', ':hover': 'transparent'},
    backgroundColor: {default: colors.surface, ':hover': colors.surface3},
    color: {default: colors.text, ':hover': colors.text},
    fontSize: type.tUi,
    fontWeight: 500,
    paddingInline: 9,
    marginInlineStart: 4,
  },
});

/**
 * The debug popover behind the ⓘ button — token counts, throughput, model.
 *
 * Opens *upward*: the action row sits at the bottom of a turn, and the last
 * turn in a thread is flush against the composer, where a downward panel would
 * have nowhere to go. There is always content above.
 */
export const debug = stylex.create({
  root: {position: 'relative', display: 'inline-flex'},
  panel: {
    position: 'absolute',
    insetBlockEnd: 'calc(100% + 6px)',
    insetInlineStart: 0,
    zIndex: 50,
    minWidth: 236,
    // A model id is the one value long enough to blow the panel out; cap the
    // width and let that row wrap rather than push past the thread column.
    maxWidth: 'min(320px, 76vw)',
    backgroundColor: colors.bg,
    backgroundImage: `linear-gradient(180deg, rgba(${channels.tint}, 0.05), rgba(${channels.tint}, 0.02))`,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    paddingBlock: 9,
    paddingInline: 11,
    boxShadow: `0 10px 34px rgba(${channels.shadow}, 0.4)`,
    cursor: 'default',
  },
  heading: {
    fontSize: type.tMicro,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: colors.textFaint,
    marginBlockEnd: 7,
  },
  list: {display: 'flex', flexDirection: 'column', gap: 4, margin: 0},
  row: {display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', gap: 18},
  label: {fontSize: type.tUi, color: colors.textDim, whiteSpace: 'nowrap'},
  value: {
    margin: 0,
    fontSize: type.tUi,
    fontFamily: type.mono,
    fontVariantNumeric: 'tabular-nums',
    color: colors.text,
    textAlign: 'end',
    overflowWrap: 'anywhere',
  },
});

/** Historical rows the removed safety gates persisted as blocked. */
export const safety = stylex.create({
  banner: {
    display: 'flex',
    flexWrap: 'wrap',
    alignItems: 'baseline',
    gap: 8,
    backgroundColor: colors.warningBg,
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
    borderRadius: 2,
    paddingBlock: 4,
    paddingInline: 9,
    fontSize: type.tUi,
    color: colors.text,
    maxWidth: '100%',
  },
  size: {color: `rgba(${channels.tint}, 0.45)`, fontSize: type.tMicro, marginInlineStart: 2},
});

/** The "browsing …" chip and the row it shares with the step count. */
export const browseChip = stylex.create({
  row: {display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap'},
  live: {
    color: colors.signalLive,
    // Slow, not attention-grabbing: it marks a state, it isn't an alert.
    animationName: kf.pulse,
    animationDuration: '2.4s',
    animationIterationCount: 'infinite',
  },
});
