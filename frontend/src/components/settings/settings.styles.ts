import * as stylex from '@stylexjs/stylex';

import {channels, colors, type} from '../../theme/tokens.stylex';

/* ── Styles for the Settings screens ───────────────────────────────────
   Seven tabs over one page language: the header and section scaffolding
   come from `page` in `../ui`, and everything here is what the tabs add
   on top — the tab strip, the tool policy rows, the config editors, the
   model grid and the sync report. */

export const settings = stylex.create({
  tabs: {
    display: 'flex',
    gap: 6,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    marginBlockEnd: 4,
    // Six tabs do not fit a phone; the strip scrolls rather than wraps,
    // which would push the content down a row. The bar itself is hidden —
    // the tabs run to the edge, which is the affordance.
    overflowX: 'auto',
    scrollbarWidth: 'none',
    '::-webkit-scrollbar': {display: 'none'},
    // `overflow-x: auto` above makes this a scroll container, which resolves
    // its `min-height: auto` to 0 — and as a flex item in the page scroller it
    // then shrinks to nothing the moment a tab's content overflows. That is
    // why Tools and Config rendered with *no tab strip at all* while the short
    // tabs looked fine. Never let it shrink.
    flexShrink: 0,
    // Those two long lists are also what you need the strip to leave, so it
    // pins to the top of the page scroller instead of scrolling away. It can
    // sit flush at 0 because `root` below moves the scroller's top padding
    // onto the header — otherwise the strip would pin *below* that padding and
    // rows would scroll through the transparent band above it. The negative
    // inline margin (cancelled by matching padding) stretches its opaque
    // background across the page gutters for the same reason.
    position: 'sticky',
    insetBlockStart: 0,
    zIndex: 3,
    backgroundColor: colors.bg,
    marginInline: {default: -40, '@media (max-width: 768px)': -16},
    paddingInline: {default: 40, '@media (max-width: 768px)': 16},
  },

  /** On `page.scroll`: hand the top padding to `chromeTop` — see `tabs`. */
  root: {paddingBlockStart: 0},
  chromeTop: {paddingBlockStart: {default: 32, '@media (max-width: 768px)': 20}},
  tab: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 7,
    paddingBlockStart: 8,
    paddingBlockEnd: 10,
    paddingInline: 12,
    fontSize: type.tBody,
    fontFamily: 'inherit',
    color: {default: colors.textDim, ':hover': colors.text},
    backgroundColor: 'transparent',
    borderStyle: 'none',
    borderBlockEndWidth: 2,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: 'transparent',
    marginBlockEnd: -1,
    cursor: 'pointer',
    whiteSpace: 'nowrap',
    textDecoration: 'none',
    transition: 'color 0.14s ease, border-color 0.14s ease',
  },
  tabActive: {color: colors.text, fontWeight: 500, borderBlockEndColor: colors.accent},

  /** Buttons that ride along a section heading, pushed to its right edge. */
  sectionActions: {marginInlineStart: 'auto', display: 'flex', gap: 8, fontWeight: 400},

  /** An icon action that rides along a row's badge line, pushed to the right. */
  rowHeadAction: {marginInlineStart: 'auto'},

  mono: {fontFamily: type.mono, fontSize: type.tUi},
  channelName: {display: 'inline-flex', alignItems: 'center', gap: 8},
  toolChips: {
    display: 'flex',
    flexWrap: 'wrap',
    gap: 5,
    paddingBlockStart: 8,
    paddingBlockEnd: 2,
  },

  filterRow: {display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'center'},
  search: {
    flexGrow: 1,
    flexShrink: 1,
    flexBasis: 220,
    maxWidth: 340,
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    paddingInline: 10,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus-within': colors.accent},
    borderRadius: 3,
    backgroundColor: colors.surface,
    color: colors.textDim,
  },
  searchInput: {
    flex: 1,
    minWidth: 0,
    borderStyle: 'none',
    outline: 'none',
    backgroundColor: 'transparent',
    color: colors.text,
    fontSize: type.tUi,
    fontFamily: 'inherit',
    paddingBlock: 8,
    paddingInline: 0,
    '::placeholder': {color: colors.textDim},
  },
  filterSelect: {flexGrow: 0, flexShrink: 0, flexBasis: 160},

  /** A one-line label / value pair in an editor form. */
  formRow: {display: 'flex', gap: 10},
  formFixed: {flexGrow: 0, flexShrink: 0, flexBasis: 'auto'},
  formGrow: {flex: 1, minWidth: 0},
  kvRow: {display: 'flex', gap: 6, alignItems: 'center'},
  kvKey: {flexGrow: 0, flexShrink: 0, flexBasis: 160},
  kvValue: {flex: 1, minWidth: 0},
  addRowBtn: {alignSelf: 'flex-start'},

  configPre: {
    margin: 0,
    paddingBlock: 10,
    paddingInline: 12,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    backgroundColor: colors.surface,
    fontFamily: type.mono,
    fontSize: type.tSmall,
    lineHeight: 1.5,
    color: colors.textDim,
    overflow: 'auto',
    maxHeight: 160,
    whiteSpace: 'pre-wrap',
    overflowWrap: 'anywhere',
  },

  /** The one-click server presets above the MCP add form. */
  presetStrip: {
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(140px, 1fr))',
    gap: 6,
  },
  preset: {
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    paddingBlock: 8,
    paddingInline: 10,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.accent},
    borderRadius: 3,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    cursor: 'pointer',
    textAlign: 'left',
    fontFamily: 'inherit',
    transition: 'border-color 0.14s ease, background 0.14s ease',
  },
  presetName: {fontSize: type.tUi, fontWeight: 600, color: colors.text},
  presetDesc: {fontSize: type.tMicro, color: colors.textDim, lineHeight: 1.3},
});

/** The models grid on Settings → Models. */
export const models = stylex.create({
  grid: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'grid',
    gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))',
    gap: 8,
  },
  card: {gap: 6},
  id: {fontFamily: type.mono, fontSize: type.tUi, overflowWrap: 'anywhere'},
  /** The single "which model is the default" control, in the filter row. */
  defaultPicker: {display: 'flex', alignItems: 'center', gap: 6, minWidth: 0},
  defaultSelect: {flexGrow: 0, flexShrink: 1, flexBasis: 260, fontFamily: type.mono},
  window: {color: colors.textDim, fontSize: type.tSmall, whiteSpace: 'nowrap'},
});

/** Settings → Models → Sync: the drift report. */
export const sync = stylex.create({
  modal: {display: 'flex', flexDirection: 'column', maxHeight: 'min(84vh, 820px)'},
  controls: {
    display: 'flex',
    gap: 8,
    alignItems: 'center',
    flexWrap: 'wrap',
    paddingBlockEnd: 4,
  },
  providerSelect: {flexGrow: 0, flexShrink: 1, flexBasis: 240},
  check: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    fontSize: type.tUi,
    color: colors.textDim,
    cursor: 'pointer',
    whiteSpace: 'nowrap',
  },
  note: {display: 'flex', alignItems: 'center', gap: 6, margin: 0},
  noteWarn: {color: colors.warn, fontSize: type.tUi},

  // The report is the scrolling region — the controls above and the add
  // button below stay reachable no matter how much drift a provider reports.
  body: {flexGrow: 1, flexShrink: 1, flexBasis: 'auto', overflowY: 'auto', minHeight: 120},
  section: {
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    paddingBlock: 10,
    borderBlockStartWidth: {default: 1, ':first-child': 0},
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
  },
  heading: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    margin: 0,
    fontSize: type.tBody,
    fontWeight: 600,
    color: colors.text,
  },
  finding: {display: 'flex', flexDirection: 'column', gap: 4},
  findingTitle: {fontSize: type.tUi, color: colors.textDim},
  plain: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  },
  plainItem: {display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', fontSize: type.tUi},
  reason: {color: colors.textDim, fontSize: type.tSmall, overflowWrap: 'anywhere'},
  /** The apply button on a context-window finding, pushed to the row's end. */
  rowEndBtn: {marginInlineStart: 'auto'},

  filter: {display: 'flex', gap: 10, alignItems: 'center', flexWrap: 'wrap'},
  list: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    overflow: 'hidden',
  },
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: 6,
    paddingInline: 10,
    borderBlockStartWidth: {default: 1, ':first-child': 0},
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    backgroundColor: {default: null, ':hover': colors.surface2},
  },
  rowMain: {display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0, cursor: 'pointer'},
  rowLabel: {
    color: colors.textDim,
    fontSize: type.tUi,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  rowMeta: {display: 'flex', alignItems: 'center', gap: 6, marginInlineStart: 'auto'},
});

/** Settings → Tools: one row per tool, grouped by family then server. */
export const tools = stylex.create({
  kind: {marginBlockStart: 18},
  kindTitle: {
    display: 'flex',
    alignItems: 'baseline',
    gap: 10,
    flexWrap: 'wrap',
    marginBlock: '0 8px',
    fontSize: type.tBody,
    fontWeight: 600,
    color: colors.text,
  },
  kindBlurb: {fontSize: type.tSmall, fontWeight: 400, color: colors.textDim},
  // Was `.tool-group + .tool-group` — a sibling combinator, so the gap moves
  // onto the group itself and the first one is exempted.
  group: {marginBlockStart: {default: 10, ':first-child': 0}},
  groupName: {
    fontFamily: type.mono,
    fontSize: type.tSmall,
    color: colors.textDim,
    paddingBlock: 4,
  },
  list: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  },

  row: {
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    gap: 14,
    paddingBlock: 8,
    paddingInline: 12,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    backgroundColor: colors.surface,
  },
  rowOff: {opacity: 0.55},
  /**
   * The config row stacks instead: a JSON blob or a comma list needs the
   * full width, unlike the two checkboxes a tool row carries.
   */
  rowStacked: {flexDirection: 'column', alignItems: 'stretch'},
  rowMain: {minWidth: 0},
  rowHead: {display: 'flex', alignItems: 'center', gap: 7, flexWrap: 'wrap'},
  rowName: {
    fontFamily: type.mono,
    fontSize: type.tUi,
    color: colors.text,
    overflowWrap: 'anywhere',
  },
  rowDesc: {marginBlock: '3px 0', fontSize: type.tSmall, lineHeight: 1.45, color: colors.textDim},
  rowControls: {display: 'flex', gap: 12, flexGrow: 0, flexShrink: 0, paddingBlockStart: 2},

  toggle: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 5,
    fontSize: type.tSmall,
    color: colors.textDim,
    cursor: 'pointer',
    userSelect: 'none',
    whiteSpace: 'nowrap',
  },
  toggleInput: {cursor: {default: 'pointer', ':disabled': 'not-allowed'}},
});

/** Settings → Maintenance: the checkpoint stats block. */
export const maint = stylex.create({
  stats: {display: 'flex', flexWrap: 'wrap', gap: 22, marginBlock: '10px 4px'},
  stat: {display: 'flex', flexDirection: 'column', gap: 2},
  dt: {
    fontSize: type.tMicro,
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
    color: colors.textDim,
  },
  dd: {margin: 0, fontSize: type.tBody, fontWeight: 600, color: colors.text},
  sub: {display: 'block', fontSize: type.tMicro, fontWeight: 400, color: colors.textDim},
  result: {
    marginBlock: '10px 0',
    paddingBlock: 8,
    paddingInline: 10,
    borderRadius: 2,
    backgroundColor: colors.surface,
    fontSize: type.tSmall,
    color: colors.textDim,
  },
});

/** The config-key row's dashed "add a new key" strip. */
export const configNew = stylex.create({
  root: {
    display: 'flex',
    gap: 8,
    flexWrap: 'wrap',
    alignItems: 'center',
    marginBlockEnd: 12,
    paddingBlock: 10,
    paddingInline: 12,
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.border,
    borderRadius: 3,
  },
  input: {flexGrow: 1, flexShrink: 1, flexBasis: 200, width: 'auto'},
});

/** Extra `badge` variants only the settings tabs use. */
export const settingsBadge = stylex.create({
  warn: {
    backgroundColor: `rgba(${channels.warn}, 0.12)`,
    color: colors.warn,
    borderColor: `rgba(${channels.warn}, 0.24)`,
  },
});
