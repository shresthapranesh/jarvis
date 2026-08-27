import * as stylex from '@stylexjs/stylex';

import {colors} from '../theme/tokens.stylex';

/* ── Styles for ArtifactsBrowser.tsx ───────────────────────────────────
   A sortable table of every artifact, with a resizable detail pane that
   opens beside it — and takes over the screen on a phone. */

export const browser = stylex.create({
  page: {display: 'flex', height: '100%', overflow: 'hidden'},
  tablePane: {flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', overflow: 'hidden'},

  header: {
    display: 'flex',
    alignItems: 'baseline',
    gap: 12,
    paddingBlockStart: 24,
    paddingBlockEnd: 16,
    paddingInline: 32,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  title: {fontSize: '1.3rem', fontWeight: 600, margin: 0, color: colors.text},
  count: {fontSize: '0.8rem', color: colors.textDim},
  empty: {paddingBlock: 40, paddingInline: 32, color: colors.textDim, fontSize: '0.9rem'},
  loading: {
    paddingBlockStart: {default: 32, '@media (max-width: 768px)': 24},
    paddingBlockEnd: 32,
    paddingInline: {default: 40, '@media (max-width: 768px)': 16},
    color: colors.textDim,
  },

  scroll: {flex: 1, overflowY: 'auto'},
  table: {width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem'},
  // Below 768px the columns stop being columns: the row becomes a card with
  // the title on its own line and the rest wrapping beneath as meta. The
  // header row carries no content once that happens, so it goes.
  thead: {display: {default: null, '@media (max-width: 768px)': 'none'}},
  th: {
    position: 'sticky',
    insetBlockStart: 0,
    zIndex: 1,
    textAlign: 'left',
    fontWeight: 600,
    fontSize: '0.66rem',
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
    paddingBlock: 10,
    paddingInline: 16,
    backgroundColor: colors.bg,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
  },
  thFirst: {paddingInlineStart: 32},

  row: {
    cursor: 'pointer',
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    backgroundColor: {default: null, ':hover': colors.surface},
    transition: 'background 0.12s',
    display: {default: null, '@media (max-width: 768px)': 'flex'},
    flexWrap: {default: null, '@media (max-width: 768px)': 'wrap'},
    alignItems: {default: null, '@media (max-width: 768px)': 'center'},
    gap: {default: null, '@media (max-width: 768px)': '6px 10px'},
    paddingBlock: {default: null, '@media (max-width: 768px)': 12},
    paddingInline: {default: null, '@media (max-width: 768px)': 16},
  },
  rowActive: {backgroundColor: {default: colors.accentDim, ':hover': colors.accentDim}},

  td: {
    paddingBlock: {default: 11, '@media (max-width: 768px)': 0},
    paddingInline: {default: 16, '@media (max-width: 768px)': 0},
    color: colors.text,
    verticalAlign: 'middle',
    display: {default: null, '@media (max-width: 768px)': 'block'},
  },
  tdFirst: {
    paddingInlineStart: {default: 32, '@media (max-width: 768px)': 0},
    flexGrow: {default: null, '@media (max-width: 768px)': 1},
    flexShrink: {default: null, '@media (max-width: 768px)': 1},
    flexBasis: {default: null, '@media (max-width: 768px)': '100%'},
  },
  cellTitle: {fontWeight: 500, display: 'flex', alignItems: 'center', gap: 8},
  cellIcon: {display: 'inline-flex', color: colors.textDim, flexShrink: 0},
  convLink: {
    color: colors.accent,
    textDecorationLine: {default: 'none', ':hover': 'underline'},
  },
  kind: {
    display: 'inline-block',
    paddingBlock: 2,
    paddingInline: 8,
    borderRadius: 4,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    fontSize: '0.7rem',
    color: colors.textDim,
  },
  muted: {color: colors.textDim},

  /**
   * The drag handle between the two panes. Its `::after` widens the hit area
   * to 13px without widening the visible 5px line.
   */
  resizer: {
    display: {default: 'block', '@media (max-width: 768px)': 'none'},
    flexGrow: 0,
    flexShrink: 0,
    flexBasis: 5,
    cursor: 'col-resize',
    backgroundColor: {default: colors.border, ':hover': colors.accent, ':active': colors.accent},
    position: 'relative',
    transition: 'background 0.12s',
    '::after': {content: '', position: 'absolute', insetBlock: 0, insetInline: -4},
  },
  detailPane: {
    display: 'flex',
    flexDirection: 'column',
    borderInlineStartWidth: 1,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.border,
    backgroundColor: colors.bg,
    overflow: 'hidden',
    // The dragged width arrives as `--detail-w` set inline, rather than as an
    // inline `flex-basis`: the phone case has to override it, and no compiled
    // class can outrank a style attribute.
    flexGrow: 0,
    flexShrink: 0,
    flexBasis: {
      default: 'var(--detail-w, clamp(360px, 42%, 620px))',
      '@media (max-width: 768px)': 'auto',
    },
    // On a phone the pane covers the table instead of splitting it.
    position: {default: null, '@media (max-width: 768px)': 'fixed'},
    inset: {default: null, '@media (max-width: 768px)': 0},
    zIndex: {default: null, '@media (max-width: 768px)': 60},
  },
  detailHeader: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    paddingBlock: 14,
    paddingInline: 16,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  detailName: {
    fontSize: '0.9rem',
    fontWeight: 600,
    color: colors.text,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
});
