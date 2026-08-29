import * as stylex from '@stylexjs/stylex';

import {channels, colors, radii, space, type} from '../theme/tokens.stylex';

/* ── Styles for ConversationList.tsx ───────────────────────────────────
   The sidebar list: its header, the bucketed sections, one row per
   conversation, and the context menu a row opens. */

/** The scrolling list and its header. */
export const list = stylex.create({
  root: {display: 'flex', flexDirection: 'column', flex: 1, overflow: 'hidden'},
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBlock: '10px 6px',
    paddingInline: 14,
    flexShrink: 0,
  },
  title: {
    fontSize: type.tMicro,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: type.trackMicro,
    color: colors.textDim,
  },
  newChat: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 22,
    height: 22,
    borderRadius: 2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: `rgba(${channels.tint}, 0.09)`, ':hover': colors.accentDim},
    backgroundColor: `rgba(${channels.tint}, 0.04)`,
    color: {default: colors.textDim, ':hover': colors.accent},
    textDecoration: 'none',
    transition: 'color 0.15s, border-color 0.15s, background 0.15s',
  },
  body: {
    flex: 1,
    overflowY: 'auto',
    paddingBlock: '4px 8px',
    '::-webkit-scrollbar': {width: 4},
    '::-webkit-scrollbar-track': {backgroundColor: 'transparent'},
    '::-webkit-scrollbar-thumb': {
      backgroundColor: `rgba(${channels.tint}, 0.12)`,
      borderRadius: 2,
    },
  },
  empty: {
    paddingBlock: space.s5,
    paddingInline: space.s4,
    fontSize: type.tSmall,
    lineHeight: 1.6,
    color: colors.textFaint,
    textAlign: 'center',
  },
  kbd: {
    fontFamily: type.mono,
    fontSize: '0.95em',
    color: colors.textDim,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    paddingBlock: 1,
    paddingInline: 4,
  },
  section: {marginBlockEnd: space.s3},
  heading: {
    fontSize: type.tMicro,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: type.trackMicro,
    color: colors.textFaint,
    paddingBlock: `${space.s2} ${space.s1}`,
    paddingInline: 14,
  },
});

/** One conversation. Publishes its hover state to three children that need it. */
export const row = stylex.create({
  // The row publishes its hover/active state as custom properties, because
  // three of its children are styled by it and no selector on a child can see
  // its parent's `:hover`.
  root: {
    display: 'flex',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 5,
    paddingBlock: 5,
    paddingInlineStart: 10,
    paddingInlineEnd: 8,
    borderRadius: radii.sm,
    marginBlock: 1,
    marginInline: 6,
    cursor: 'pointer',
    borderInlineStartWidth: 2,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: {default: 'transparent', ':hover': colors.borderStrong},
    backgroundColor: {default: 'transparent', ':hover': `rgba(${channels.tint}, 0.06)`},
    transition:
      'background 0.18s cubic-bezier(0.2, 0.8, 0.2, 1), border-color 0.18s cubic-bezier(0.2, 0.8, 0.2, 1)',
    '--conv-title-color': {default: colors.textDim, ':hover': colors.text},
    '--conv-menu-opacity': {default: '0', ':hover': '1'},
    '--conv-time-max-w': {default: '0', ':hover': '5rem', ':focus-within': '5rem'},
    '--conv-time-ml': {default: '0', ':hover': space.s2, ':focus-within': space.s2},
    '--conv-time-opacity': {default: '0', ':hover': '1', ':focus-within': '1'},
  },
  // The row publishes its hover/active state as custom properties, because
  // three of its children are styled by it and no selector on a child can see
  // its parent's `:hover`.
  active: {
    backgroundColor: colors.accentDim,
    borderInlineStartColor: colors.accent,
    '--conv-title-color': colors.text,
    '--conv-menu-opacity': '1',
  },
  // The row publishes its hover/active state as custom properties, because
  // three of its children are styled by it and no selector on a child can see
  // its parent's `:hover`.
  menuOpen: {'--conv-menu-opacity': '1'},
  link: {
    flex: 1,
    minWidth: 0,
    display: 'flex',
    flexDirection: 'row',
    alignItems: 'baseline',
    textDecoration: 'none',
  },
  title: {
    flex: 1,
    minWidth: 0,
    fontSize: type.tUi,
    color: 'var(--conv-title-color)',
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  titleActive: {fontWeight: 500},

  /* Collapsed to zero width until hover — otherwise it reserves horizontal
     space in every row and truncates titles that would otherwise fit. */
  time: {
    flex: '0 0 auto',
    fontFamily: type.mono,
    fontSize: type.tMicro,
    color: colors.textFaint,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    maxWidth: 'var(--conv-time-max-w)',
    marginInlineStart: 'var(--conv-time-ml)',
    opacity: 'var(--conv-time-opacity)',
    transition:
      'max-width 0.18s cubic-bezier(0.2, 0.8, 0.2, 1), margin-left 0.18s cubic-bezier(0.2, 0.8, 0.2, 1), opacity 0.18s',
  },
  titleInput: {
    flex: 1,
    minWidth: 0,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.accentDim,
    borderRadius: 2,
    color: colors.text,
    fontSize: type.tSmall,
    paddingBlock: 2,
    paddingInline: 6,
    outline: 'none',
  },
  menuBtn: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 22,
    height: 22,
    flexShrink: 0,
    backgroundColor: 'transparent',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: 'transparent', ':hover': colors.accentDim},
    borderRadius: 2,
    color: {default: colors.textDim, ':hover': colors.accent},
    cursor: 'pointer',
    padding: 0,
    opacity: 'var(--conv-menu-opacity)',
    transition: 'opacity 0.15s, color 0.15s, border-color 0.15s',
  },
  menuBtnOpen: {color: colors.accent, borderColor: colors.accentDim},
});

/** The pin / rename / delete popover, portalled to the body. */
export const menu = stylex.create({
  root: {
    position: 'fixed',
    transform: 'translateX(-100%)',
    minWidth: 130,
    backgroundImage: `linear-gradient(180deg, rgba(${channels.tint}, 0.05), rgba(${channels.tint}, 0.02))`,
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    boxShadow: `0 8px 28px rgba(${channels.shadow}, 0.45)`,
    padding: 4,
    zIndex: 200,
    display: 'flex',
    flexDirection: 'column',
  },
  item: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: 6,
    paddingInline: 9,
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    borderStyle: 'none',
    borderRadius: 2,
    color: colors.text,
    fontSize: type.tSmall,
    textAlign: 'left',
    cursor: 'pointer',
    transition: 'background 0.12s, color 0.12s',
    '--menu-icon-color': colors.textDim,
  },
  itemDanger: {
    backgroundColor: {default: 'transparent', ':hover': `rgba(${channels.danger}, 0.1)`},
    color: {default: colors.text, ':hover': colors.danger},
    '--menu-icon-color': {default: colors.textDim, ':hover': colors.danger},
  },
  icon: {flexShrink: 0, color: 'var(--menu-icon-color)'},
});
