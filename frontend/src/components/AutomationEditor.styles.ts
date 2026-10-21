import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, css, type} from '../theme/tokens.stylex';

/* ── Styles for AutomationEditor ───────────────────────────────────────
   `sheet` is the frame (backdrop, header, scrolling body, Save bar);
   `typeCard` the four-way type picker; `ed` the fields and rail cards.

   The two-column split is a container query on the body, not a media
   query: the sheet is resizable, so the viewport says nothing about how
   much room the columns have. */

const PHONE = '@media (max-width: 860px)';
const WIDE = '@container (min-width: 700px)';

export const sheet = stylex.create({
  backdrop: {
    position: 'fixed',
    inset: 0,
    // A full-viewport blur is the one backdrop-filter cost worth caring about
    // on a mid-range phone; touch trades it for a heavier tint.
    backgroundColor: {
      default: `rgba(${channels.shadow}, 0.6)`,
      '@media (hover: none)': `rgba(${channels.shadow}, 0.72)`,
    },
    backdropFilter: {default: 'blur(4px)', '@media (hover: none)': 'none'},
    WebkitBackdropFilter: {default: 'blur(4px)', '@media (hover: none)': 'none'},
    zIndex: 42,
    animationName: kf.fadeIn,
    animationDuration: '0.18s',
  },
  root: {
    position: 'fixed',
    insetBlock: 0,
    insetInlineEnd: 0,
    // `--sheet-w` is the dragged width; a phone always gets the full screen.
    width: {default: 'var(--sheet-w, 960px)', [PHONE]: '100%'},
    maxWidth: '100%',
    paddingBlockStart: {default: null, [PHONE]: css.safeTop},
    paddingBlockEnd: {default: null, [PHONE]: css.safeBottom},
    backgroundColor: colors.bg,
    borderInlineStartWidth: {default: 1, [PHONE]: 0},
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.border,
    boxShadow: `-8px 0 40px rgba(${channels.shadow}, 0.5)`,
    display: 'flex',
    flexDirection: 'column',
    zIndex: 52,
    animationName: kf.slideIn,
    animationDuration: '0.22s',
    animationTimingFunction: 'ease',
  },

  header: {
    display: 'flex',
    alignItems: 'flex-start',
    gap: 14,
    paddingBlock: '20px 16px',
    paddingInline: {default: 28, [PHONE]: 16},
    flexShrink: 0,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
  },
  headerIcon: {width: 40, height: 40, marginBlockStart: 16},
  titleBlock: {display: 'flex', flexDirection: 'column', gap: 2, flex: 1, minWidth: 0},
  eyebrow: {
    fontSize: type.tMicro,
    textTransform: 'uppercase',
    letterSpacing: type.trackMicro,
    color: colors.textFaint,
    marginBlockEnd: 2,
  },
  nameInput: {
    fontFamily: type.display,
    fontSize: type.tPage,
    fontWeight: 500,
    color: colors.text,
    backgroundColor: {default: 'transparent', ':hover': colors.surface, ':focus': colors.surface},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: 'transparent', ':focus': colors.borderStrong},
    borderRadius: 2,
    paddingBlock: 3,
    paddingInline: 6,
    marginInline: -7,
    outline: 'none',
    width: '100%',
    transition: 'background 0.12s, border-color 0.12s',
  },
  descInput: {
    fontFamily: 'inherit',
    fontSize: type.tUi,
    color: colors.textDim,
    backgroundColor: {default: 'transparent', ':hover': colors.surface, ':focus': colors.surface},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: 'transparent', ':focus': colors.borderStrong},
    borderRadius: 2,
    paddingBlock: 3,
    paddingInline: 6,
    marginInline: -7,
    outline: 'none',
    width: '100%',
    transition: 'background 0.12s, border-color 0.12s',
  },
  headerActions: {
    display: 'flex',
    alignItems: 'center',
    gap: 14,
    marginBlockStart: 2,
    flexShrink: 0,
  },

  body: {
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    containerType: 'inline-size',
    '::-webkit-scrollbar': {width: 6},
    '::-webkit-scrollbar-thumb': {backgroundColor: colors.border, borderRadius: 3},
  },
  grid: {
    display: 'grid',
    gridTemplateColumns: {default: 'minmax(0, 1fr)', [WIDE]: 'minmax(0, 1fr) 320px'},
    gap: {default: 20, [WIDE]: 28},
    paddingBlock: 22,
    paddingInline: {default: 16, [WIDE]: 28},
    alignItems: 'start',
  },
  // Its own container too, so the type cards size to the column, not the sheet.
  main: {
    display: 'flex',
    flexDirection: 'column',
    gap: 20,
    minWidth: 0,
    containerType: 'inline-size',
  },
  rail: {
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
    minWidth: 0,
    position: {default: 'static', [WIDE]: 'sticky'},
    insetBlockStart: 0,
  },

  footer: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: 12,
    paddingInline: {default: 28, [PHONE]: 16},
    flexShrink: 0,
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    backgroundColor: colors.bg,
  },
  footerStatus: {
    flex: 1,
    minWidth: 0,
    fontSize: type.tUi,
    color: colors.textDim,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  footerError: {color: colors.errorText},
  kbd: {
    display: {default: 'inline', '@media (hover: none)': 'none'},
    fontFamily: type.mono,
    fontSize: type.tMicro,
    color: colors.textFaint,
    marginInlineEnd: 4,
  },
  save: {paddingInline: 16},
  loading: {padding: 28, fontSize: type.tUi, color: colors.textDim},
});

export const typeCard = stylex.create({
  grid: {
    display: 'grid',
    gridTemplateColumns: {
      default: 'repeat(2, minmax(0, 1fr))',
      '@container (min-width: 460px)': 'repeat(4, minmax(0, 1fr))',
    },
    gap: 8,
  },
  card: {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    paddingBlock: 10,
    paddingInline: 12,
    textAlign: 'start',
    fontFamily: 'inherit',
    color: colors.text,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: 'transparent',
    borderRadius: 3,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    outline: {default: 'none', ':focus-visible': `2px solid ${colors.accent}`},
    outlineOffset: 2,
    transition: 'background 0.12s, border-color 0.12s',
  },
  on: {
    backgroundColor: {default: colors.surface2, ':hover': colors.surface2},
    borderColor: colors.borderStrong,
  },
  head: {display: 'flex', alignItems: 'center', gap: 8, minWidth: 0},
  icon: {width: 24, height: 24},
  label: {fontSize: type.tUi, fontWeight: 600},
  blurb: {fontSize: type.tSmall, color: colors.textDim, lineHeight: 1.35},
});

export const ed = stylex.create({
  block: {display: 'flex', flexDirection: 'column', gap: 6},
  labelNote: {color: colors.textFaint, marginInlineStart: 4},
  prompt: {
    minHeight: {default: 360, [PHONE]: 240},
    fontSize: type.tBody,
    lineHeight: 1.6,
    paddingBlock: 12,
    paddingInline: 14,
  },
  code: {
    minHeight: {default: 420, [PHONE]: 280},
    fontFamily: type.mono,
    fontSize: type.tUi,
    lineHeight: 1.55,
    tabSize: 4,
    whiteSpace: 'pre',
    overflowX: 'auto',
    paddingBlock: 12,
    paddingInline: 14,
  },
  mono: {fontFamily: type.mono, fontSize: type.tUi},
  request: {display: 'flex', gap: 6},
  method: {flex: '0 0 104px', fontFamily: type.mono, fontSize: type.tUi},
  url: {flex: 1, minWidth: 0, fontFamily: type.mono, fontSize: type.tUi},
  invalid: {borderColor: {default: colors.danger, ':focus': colors.danger}},

  card: {
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
    padding: 14,
    borderRadius: 3,
    backgroundColor: colors.surface,
  },
  cardTitle: {
    margin: 0,
    fontSize: type.tMicro,
    fontWeight: 500,
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
  },
  smallLabel: {fontSize: type.tSmall, color: colors.textDim},
  option: {display: 'flex', flexDirection: 'column', gap: 4},
  optionLabel: {color: colors.text},
  optionHint: {
    fontSize: type.tSmall,
    color: colors.textDim,
    lineHeight: 1.45,
    paddingInlineStart: 38,
  },
});
