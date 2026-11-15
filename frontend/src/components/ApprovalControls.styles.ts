import * as stylex from '@stylexjs/stylex';

import {colors, type} from '../theme/tokens.stylex';

/* ── Styles for ApprovalControls.tsx ───────────────────────────────────
   A request waiting on a human: what asks, the question, and the answer
   row. */

/** The `N pending` total and the per-source breakdown beside it. */
export const card = stylex.create({
  argsToggle: {
    fontSize: type.tSmall,
    fontFamily: 'inherit',
    backgroundColor: 'transparent',
    borderStyle: 'none',
    padding: 0,
    color: colors.textDim,
    cursor: 'pointer',
    textDecorationLine: 'underline',
  },
  argsPre: {
    marginBlock: '6px 0',
    marginInline: 0,
    paddingBlock: 8,
    paddingInline: 10,
    backgroundColor: colors.surface2,
    borderRadius: 2,
    fontSize: type.tSmall,
    maxHeight: 220,
    overflow: 'auto',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
  },
});

export const answer = stylex.create({
  row: {display: 'flex', alignItems: 'center', gap: 8, marginBlockStart: 8},
  input: {
    flex: 1,
    minWidth: 0,
    fontSize: type.tUi,
    fontFamily: 'inherit',
    paddingBlock: 7,
    paddingInline: 10,
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 2,
    color: colors.text,
    outline: 'none',
  },
  btn: {
    fontSize: type.tUi,
    fontFamily: 'inherit',
    paddingBlock: 7,
    paddingInline: 16,
    borderRadius: 2,
    borderWidth: 1,
    borderStyle: 'solid',
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.5},
    flexShrink: 0,
    filter: {default: null, ':hover:not(:disabled)': 'brightness(1.15)'},
    transition: 'filter 0.12s',
  },
  approve: {
    backgroundColor: colors.accent,
    borderColor: colors.accent,
    color: colors.accentContrast,
  },
  deny: {
    backgroundColor: colors.errorBg,
    borderColor: 'transparent',
    color: colors.errorText,
  },
});
