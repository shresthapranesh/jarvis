import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, css, type} from '../theme/tokens.stylex';

/* ── Styles for ArtifactPanel.tsx ──────────────────────────────────────
   The side panel and its artifact list, the detail pane below it, the
   version-history drawer, and the line diff that drawer can open. */

/** The sheet — a side panel on a desktop, the whole screen below 860px. */
export const panel = stylex.create({
  root: {
    position: 'fixed',
    insetBlock: 0,
    insetInlineEnd: 0,
    // At full width the glass has nothing left to sit over — the thread reads
    // straight through it — and a full-viewport blur costs a frame on a phone.
    width: {default: 480, '@media (max-width: 860px)': '100%'},
    maxWidth: {default: '90vw', '@media (max-width: 860px)': '100%'},
    minWidth: {default: null, '@media (max-width: 860px)': 0},
    paddingBlockStart: {default: null, '@media (max-width: 860px)': css.safeTop},
    paddingBlockEnd: {default: null, '@media (max-width: 860px)': css.safeBottom},
    backgroundColor: {default: colors.glassBg, '@media (max-width: 860px)': colors.bg},
    backdropFilter: {default: 'blur(14px)', '@media (max-width: 860px)': 'none'},
    WebkitBackdropFilter: {default: 'blur(14px)', '@media (max-width: 860px)': 'none'},
    borderInlineStartWidth: {default: 1, '@media (max-width: 860px)': 0},
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.glassBorder,
    boxShadow: `-4px 0 28px rgba(${channels.shadow}, 0.45)`,
    display: 'flex',
    flexDirection: 'column',
    zIndex: 50,
    animationName: kf.panelEnter,
    animationDuration: '0.22s',
    animationTimingFunction: 'ease',
    animationFillMode: 'forwards',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBlock: 14,
    paddingInline: 16,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  title: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    fontSize: type.tBody,
    fontWeight: 600,
    color: colors.text,
  },

  list: {
    display: 'flex',
    flexDirection: 'column',
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    maxHeight: '30%',
    overflowY: 'auto',
    flexShrink: 0,
  },
  item: {
    textAlign: 'left',
    backgroundColor: {default: 'transparent', ':hover': colors.surface},
    borderStyle: 'none',
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    paddingBlock: 10,
    paddingInline: 16,
    color: colors.text,
    cursor: 'pointer',
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    transition: 'background 0.12s',
  },
  itemActive: {
    backgroundColor: colors.accentDim,
    borderInlineStartWidth: 2,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.accent,
    paddingInlineStart: 14,
  },
  itemTitle: {
    fontSize: type.tBody,
    fontWeight: 500,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  itemMeta: {fontSize: type.tMicro, color: colors.textDim},

  empty: {
    paddingBlock: 32,
    paddingInline: 16,
    textAlign: 'center',
    color: colors.textDim,
    fontSize: type.tUi,
  },
});

/** The selected artifact: toolbar, title, and the rendered body. */
export const detail = stylex.create({
  root: {
    flex: 1,
    overflowY: 'auto',
    paddingBlock: 16,
    paddingInline: 20,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
  },
  toolbar: {display: 'flex', gap: 6, flexWrap: 'wrap'},
  title: {fontSize: type.tTitle, fontWeight: 600, marginBlock: '4px 2px', color: colors.text},
  content: {fontSize: type.tBody, lineHeight: 1.55},
  media: {maxWidth: '100%', width: '100%', borderRadius: 3},
});

/** The inline editor for a markdown artifact. */
export const editor = stylex.create({
  root: {display: 'flex', flexDirection: 'column', gap: 10, flex: 1, minHeight: 0},
  title: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    color: colors.text,
    fontSize: type.tBody,
    fontWeight: 600,
    paddingBlock: 8,
    paddingInline: 12,
    outline: 'none',
  },
  content: {
    flex: 1,
    minHeight: 320,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    color: colors.text,
    fontFamily: type.mono,
    fontSize: type.tUi,
    lineHeight: 1.5,
    paddingBlock: 10,
    paddingInline: 12,
    resize: 'vertical',
    outline: 'none',
  },
});

/** Version history — pick any two versions to compare, or restore an old one. */
export const version = stylex.create({
  panel: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    paddingBlock: 10,
    paddingInline: 12,
    marginBlock: '8px 12px',
    display: 'flex',
    flexDirection: 'column',
    gap: 10,
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    fontSize: type.tBody,
  },
  hint: {fontSize: type.tSmall, opacity: 0.7},
  list: {display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 200, overflowY: 'auto'},
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: 6,
    paddingInline: 8,
    borderRadius: 2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: 'transparent',
    fontSize: type.tUi,
  },
  rowActive: {borderColor: colors.accentDim, backgroundColor: colors.accentDim},
  badge: {
    fontSize: type.tMicro,
    fontWeight: 700,
    paddingBlock: 2,
    paddingInline: 6,
    borderRadius: 2,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    minWidth: 36,
    textAlign: 'center',
  },
  title: {flex: 1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis'},
  meta: {fontSize: type.tSmall, color: colors.textDim, flexShrink: 0},
  actions: {display: 'flex', gap: 4, marginInlineStart: 'auto'},
});

/** The line diff between two versions. */
export const diff = stylex.create({
  root: {
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    paddingBlockStart: 8,
  },
  header: {
    display: 'flex',
    justifyContent: 'space-between',
    alignItems: 'center',
    fontSize: type.tUi,
    fontWeight: 600,
    marginBlockEnd: 6,
  },
  content: {
    maxHeight: 300,
    overflowY: 'auto',
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    padding: 8,
    fontFamily: type.mono,
    fontSize: type.tSmall,
    lineHeight: 1.4,
    whiteSpace: 'pre-wrap',
  },
  add: {backgroundColor: `rgba(${channels.ok}, 0.12)`, color: colors.ok},
  del: {
    backgroundColor: `rgba(${channels.danger}, 0.12)`,
    color: colors.danger,
    textDecorationLine: 'line-through',
  },
});
