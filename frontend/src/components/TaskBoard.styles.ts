import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {colors, space, type} from '../theme/tokens.stylex';

/* ── Styles for TaskBoard.tsx ──────────────────────────────────────────
   The kanban: five status columns of cards, each card carrying its chips,
   its live token tail while running, and an answer box when it is blocked
   on a question. */

export const board = stylex.create({
  page: {
    display: 'flex',
    flexDirection: 'column',
    height: '100%',
    overflow: 'hidden',
    paddingBlockStart: {default: 32, '@media (max-width: 768px)': space.s4},
    paddingBlockEnd: {default: 16, '@media (max-width: 768px)': space.s2},
    paddingInline: {default: 40, '@media (max-width: 768px)': space.s4},
    gap: 16,
  },
  columns: {
    display: 'flex',
    gap: 12,
    alignItems: 'flex-start',
    flex: 1,
    minHeight: 0,
    overflowX: 'auto',
    paddingBlockEnd: 8,
    // The row already scrolls; snapping just stops it resting mid-column.
    scrollSnapType: {default: null, '@media (max-width: 768px)': 'x mandatory'},
  },
  col: {
    // Flex-basis over min-width on a phone, so one column fills the screen
    // with a peek of the next — the peek is what says there is more.
    flexGrow: {default: 1, '@media (max-width: 768px)': 0},
    flexShrink: {default: 1, '@media (max-width: 768px)': 0},
    flexBasis: {default: 0, '@media (max-width: 768px)': '82%'},
    minWidth: {default: 210, '@media (max-width: 768px)': null},
    scrollSnapAlign: {default: null, '@media (max-width: 768px)': 'start'},
    maxHeight: '100%',
    display: 'flex',
    flexDirection: 'column',
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    padding: 10,
  },
  colHead: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'baseline',
    fontFamily: type.mono,
    fontSize: type.tMicro,
    fontWeight: 500,
    letterSpacing: '0.08em',
    textTransform: 'uppercase',
    color: colors.textDim,
    paddingInline: 2,
    paddingBlockEnd: 8,
  },
  // The old rules reached the title through `.board-col--running
  // .board-col-head span:first-child`; the column knows its own key, so the
  // tint travels straight to the element that wants it.
  colTitleRunning: {color: colors.accentStrong},
  colTitleBlocked: {color: colors.warningText},
  colTitleDone: {color: colors.ok},
  colCount: {fontSize: type.tMicro, color: colors.textFaint},

  cards: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    overflowY: 'auto',
    minHeight: 24,
  },
});

export const card = stylex.create({
  root: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    padding: 10,
  },
  running: {borderColor: colors.accent, boxShadow: `0 0 0 1px ${colors.accentDim}`},
  blocked: {borderColor: colors.warningBorder},

  title: {fontSize: type.tUi, fontWeight: 600, color: colors.text, lineHeight: 1.35},
  meta: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 4,
    marginBlockStart: 5,
    // A card with no chips would otherwise still pay the 5px.
    ':empty': {display: 'none'},
  },
  body: {
    marginBlock: '6px 0',
    fontSize: type.tSmall,
    color: colors.textDim,
    lineHeight: 1.45,
    display: '-webkit-box',
    WebkitLineClamp: 3,
    WebkitBoxOrient: 'vertical',
    overflow: 'hidden',
  },
  reason: {
    marginBlock: '6px 0',
    fontSize: type.tSmall,
    color: colors.warningText,
    lineHeight: 1.4,
    wordBreak: 'break-word',
  },
  /** A blocked-on-a-question reason reads as a prompt, so it gets a card. */
  reasonQuestion: {
    color: colors.text,
    backgroundColor: colors.warningBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.warningBorder,
    borderRadius: 3,
    paddingBlock: 7,
    paddingInline: 9,
  },
  summary: {
    marginBlock: '6px 0',
    fontSize: type.tSmall,
    color: colors.textDim,
    lineHeight: 1.4,
    display: '-webkit-box',
    WebkitLineClamp: 3,
    WebkitBoxOrient: 'vertical',
    overflow: 'hidden',
  },
  actions: {display: 'flex', alignItems: 'center', gap: 3, marginBlockStart: 8},
  transcript: {
    marginInlineStart: 'auto',
    fontSize: type.tMicro,
    color: {default: colors.accent, ':hover': colors.accentStrong},
    textDecorationLine: {default: 'none', ':hover': 'underline'},
  },

  /** The live token tail while the task's run is streaming. */
  tail: {
    marginBlock: '6px 0',
    fontSize: type.tMicro,
    fontFamily: type.mono,
    color: colors.textDim,
    lineHeight: 1.45,
    maxHeight: 72,
    overflow: 'hidden',
    wordBreak: 'break-word',
    borderInlineStartWidth: 2,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.accentDim,
    paddingInlineStart: 7,
  },
  tailCursor: {
    display: 'inline-block',
    width: 6,
    height: 11,
    marginInlineStart: 3,
    backgroundColor: colors.accent,
    verticalAlign: 'baseline',
    animationName: kf.blink,
    animationDuration: '1s',
    animationTimingFunction: 'steps(2)',
    animationIterationCount: 'infinite',
  },
});

export const chip = stylex.create({
  base: {
    fontSize: type.tNano,
    color: colors.textDim,
    backgroundColor: colors.surface3,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    paddingBlock: 1,
    paddingInline: 7,
    whiteSpace: 'nowrap',
  },
  priority: {
    color: colors.accentStrong,
    backgroundColor: colors.accentDim,
    borderColor: 'transparent',
  },
  danger: {
    color: colors.errorText,
    backgroundColor: colors.errorBg,
    borderColor: colors.errorBorder,
  },
  question: {
    color: colors.warningText,
    backgroundColor: colors.warningBg,
    borderColor: colors.warningBorder,
  },
});

/** The answer box a `needs_input` card grows. */
export const answer = stylex.create({
  root: {display: 'flex', flexDirection: 'column', gap: 6, marginBlockStart: 8},
  input: {
    width: '100%',
    resize: 'vertical',
    fontSize: type.tSmall,
    fontFamily: type.body,
    color: colors.text,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.borderStrong, ':focus': colors.accent},
    borderRadius: 3,
    paddingBlock: 7,
    paddingInline: 9,
    outline: 'none',
  },
  btn: {alignSelf: 'flex-end', fontSize: type.tMicro},
});

/** The dependency picker in the create / edit modal. */
export const parents = stylex.create({
  list: {
    listStyle: 'none',
    margin: 0,
    padding: 6,
    maxHeight: 160,
    overflowY: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
  },
  option: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    fontSize: type.tSmall,
    color: colors.text,
    paddingBlock: 4,
    paddingInline: 6,
    borderRadius: 2,
    backgroundColor: {default: null, ':hover': colors.surface2},
    cursor: 'pointer',
  },
  dot: {
    width: 7,
    height: 7,
    borderRadius: '50%',
    flexShrink: 0,
    backgroundColor: colors.textFaint,
  },
  dotRunning: {backgroundColor: colors.accent},
  dotDone: {backgroundColor: colors.ok},
  dotBlocked: {backgroundColor: colors.warningText},

  priorityInput: {maxWidth: 110},
});

export function parentDotStyle(status: string) {
  if (status === 'running') return parents.dotRunning;
  if (status === 'done') return parents.dotDone;
  if (status === 'blocked') return parents.dotBlocked;
  return null;
}
