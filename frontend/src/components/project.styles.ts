import * as stylex from '@stylexjs/stylex';

import {colors, type} from '../theme/tokens.stylex';

/* ── Styles for ProjectsView.tsx and ProjectDetail.tsx ─────────────────
   The index is a grid of cards; the detail is a two-column page whose
   right side lists the project's conversations. The card itself is the
   skill card from `memory.styles` — only the grid and the folder-icon
   tint are project-specific. */

export const projects = stylex.create({
  grid: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))',
    gap: 12,
  },
  card: {cursor: 'pointer'},
  cardName: {display: 'inline-flex', alignItems: 'center', gap: 7},
  /** The folder glyph, in both the card name and the detail title. */
  icon: {color: colors.accent, flexShrink: 0},
});

export const detail = stylex.create({
  page: {gap: 18},
  breadcrumb: {fontSize: type.tSmall, color: colors.textFaint, marginBlockEnd: 2},
  breadcrumbLink: {
    color: {default: colors.textDim, ':hover': colors.text},
    textDecorationLine: 'none',
  },
  title: {display: 'flex', alignItems: 'center', gap: 9},

  // Instructions and memory side by side on a desktop; stacked once there
  // isn't room for two readable columns.
  grid: {
    display: 'grid',
    gridTemplateColumns: {default: '1fr 1fr', '@media (max-width: 900px)': '1fr'},
    gap: 18,
  },
  sectionHint: {fontSize: type.tSmall, color: colors.textFaint, margin: 0},
  textarea: {fontSize: type.tUi, lineHeight: 1.5, resize: 'vertical', minHeight: 120},
  sectionActions: {display: 'flex', justifyContent: 'flex-end'},
  addExistingBtn: {marginInlineStart: 'auto', fontSize: type.tSmall},

  convList: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
  },
  convRow: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    paddingBlock: 8,
    paddingInline: 12,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.borderStrong},
    borderRadius: 3,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
  },
  convLink: {
    flex: 1,
    minWidth: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    textDecorationLine: 'none',
    color: 'inherit',
  },
  convTitle: {
    fontSize: type.tBody,
    color: colors.text,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },

  footer: {marginBlockStart: 'auto'},
  newChatHint: {
    fontSize: type.tSmall,
    color: colors.textFaint,
    marginBlock: '0 6px',
    marginInlineStart: 2,
  },
});
