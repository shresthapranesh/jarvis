import * as stylex from '@stylexjs/stylex';

import {channels, colors, space} from '../theme/tokens.stylex';

/* ── Styles for routes/logs.tsx ────────────────────────────────────────
   A live tail of the server log: a filter toolbar, then a monospace grid
   of rows tinted by level. */

export const logs = stylex.create({
  page: {
    display: 'flex',
    flexDirection: 'column',
    height: '100%',
    paddingBlock: 16,
    paddingInline: {default: 18, '@media (max-width: 768px)': space.s3},
    gap: 12,
  },
  header: {display: 'flex', flexDirection: 'column', gap: 10, flexShrink: 0},
  titleRow: {display: 'flex', alignItems: 'baseline', gap: 14},
  title: {margin: 0, fontSize: '1.1rem', fontWeight: 600},
  status: {fontSize: '0.75rem', color: colors.textDim},
  statusOk: {color: colors.ok},
  statusErr: {color: colors.errorText},

  toolbar: {display: 'flex', flexWrap: 'wrap', gap: 10, alignItems: 'end'},
  field: {
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
    fontSize: '0.72rem',
    color: colors.textDim,
  },
  fieldGrow: {flex: 1, minWidth: 220},
  // The old rule was `.logs-field select, .logs-field input`; the control
  // now carries the style itself.
  control: {
    backgroundColor: colors.surface,
    color: colors.text,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 6,
    paddingBlock: 6,
    paddingInline: 8,
    fontSize: '0.85rem',
    fontFamily: 'inherit',
    minWidth: 110,
  },
  controlGrow: {width: '100%'},
  btn: {
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    color: colors.text,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 6,
    paddingBlock: 6,
    paddingInline: 12,
    fontSize: '0.82rem',
    fontFamily: 'inherit',
    cursor: 'pointer',
  },
  btnActive: {
    backgroundColor: {default: colors.accentDim, ':hover': colors.accentDim},
    borderColor: colors.accent,
    color: colors.accent,
  },

  body: {
    flex: 1,
    minHeight: 0,
    overflowY: 'auto',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 8,
    backgroundColor: `rgba(${channels.shadow}, 0.25)`,
  },
  empty: {padding: 32, textAlign: 'center', color: colors.textDim, fontSize: '0.85rem'},
  list: {
    margin: 0,
    paddingBlock: 4,
    paddingInline: 0,
    listStyle: 'none',
    fontFamily: "ui-monospace, 'SF Mono', Menlo, Consolas, monospace",
    fontSize: '0.78rem',
  },

  row: {
    display: 'grid',
    // 420px of fixed track before the message column gets anything, which
    // overflows any phone — and fixed tracks do not shrink. Below 768px the
    // meta takes one line and the message wraps onto the next.
    gridTemplateColumns: {
      default: '156px 64px 200px 1fr',
      '@media (max-width: 768px)': 'auto auto minmax(0, 1fr)',
    },
    rowGap: {default: null, '@media (max-width: 768px)': 2},
    columnGap: 8,
    paddingBlock: {default: 2, '@media (max-width: 768px)': 6},
    paddingInline: 12,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: `rgba(${channels.tint}, 0.03)`,
    alignItems: 'baseline',
    lineHeight: 1.45,
    backgroundColor: {default: null, ':hover': colors.surface},
  },
  rowWarning: {backgroundColor: 'rgba(180, 110, 30, 0.05)'},
  rowError: {backgroundColor: 'rgba(120, 30, 30, 0.08)'},

  cell: {whiteSpace: 'pre-wrap', wordBreak: 'break-word'},
  cellTs: {color: colors.textDim},
  // The level's colour used to come from `.logs-row--info .logs-cell--level`;
  // the row's level is known here, so it travels straight to the cell.
  cellLevel: {fontWeight: 600},
  levelDebug: {color: '#6b7280'},
  levelInfo: {color: colors.accent},
  levelWarning: {color: colors.warningText},
  levelError: {color: colors.errorText},
  cellLogger: {
    color: colors.textDim,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  cellMsg: {gridColumn: {default: null, '@media (max-width: 768px)': '1 / -1'}},
});

export function levelStyle(level: string) {
  const l = level.toLowerCase();
  if (l === 'debug') return logs.levelDebug;
  if (l === 'info') return logs.levelInfo;
  if (l === 'warning') return logs.levelWarning;
  if (l === 'error' || l === 'critical') return logs.levelError;
  return null;
}

export function rowStyle(level: string) {
  const l = level.toLowerCase();
  if (l === 'warning') return logs.rowWarning;
  if (l === 'error' || l === 'critical') return logs.rowError;
  return null;
}
