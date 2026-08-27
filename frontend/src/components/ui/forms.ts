/* ════════════════════════════════════════════════════════════════════
   Form controls — one visual treatment, three elements.
   ════════════════════════════════════════════════════════════════════ */
import * as stylex from '@stylexjs/stylex';

import {colors, type} from '../../theme/tokens.stylex';

// Repeated rather than spread from a shared const: `stylex.create` is compiled
// away at build time, so every value in it has to be a literal the plugin can
// see — a spread of an outside object is not.
export const field = stylex.create({
  group: {display: 'flex', flexDirection: 'column', gap: 5},
  label: {
    fontSize: '0.68rem',
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
  },
  input: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 7,
    color: colors.text,
    fontSize: '0.85rem',
    fontFamily: 'inherit',
    paddingBlock: 8,
    paddingInline: 10,
    outline: 'none',
    transition: 'border-color 0.15s',
  },
  textarea: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 7,
    color: colors.text,
    fontSize: '0.85rem',
    fontFamily: 'inherit',
    paddingBlock: 8,
    paddingInline: 10,
    outline: 'none',
    transition: 'border-color 0.15s',
    resize: 'vertical',
    lineHeight: 1.5,
  },
  select: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 7,
    color: colors.text,
    fontSize: '0.85rem',
    fontFamily: 'inherit',
    paddingBlock: 8,
    paddingInline: 10,
    outline: 'none',
    transition: 'border-color 0.15s',
    cursor: 'pointer',
  },
  hint: {fontSize: '0.78rem', color: colors.textDim, marginBlockStart: 4},
});

/**
 * Page scaffolding. `page.root` is the flex column every route mounts as;
 * `page.scroll` is the padded, scrolling variant most settings-like screens
 * use directly.
 */

/**
 * The monospace value editor — a config key's value, a project's memory
 * blob. Distinct from `field` on purpose: these hold text a machine wrote,
 * so they are mono, denser, and sit on `surface` rather than `surface2`.
 */
export const codeField = stylex.create({
  key: {fontFamily: type.mono, fontSize: '0.72rem', color: colors.textDim},
  input: {
    width: '100%',
    paddingBlock: 6,
    paddingInline: 9,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 6,
    backgroundColor: colors.surface,
    color: colors.text,
    fontFamily: type.mono,
    fontSize: '0.78rem',
    outline: 'none',
    boxSizing: 'border-box',
  },
  multiline: {resize: 'vertical', lineHeight: 1.5},
  readonly: {
    marginBlock: '8px 0',
    paddingBlock: 8,
    paddingInline: 10,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 6,
    backgroundColor: colors.surface,
    color: colors.textDim,
    fontFamily: type.mono,
    fontSize: '0.74rem',
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-all',
    maxHeight: 180,
    overflow: 'auto',
  },
  edit: {display: 'flex', flexDirection: 'column', gap: 8, marginBlockStart: 8},
  actions: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexWrap: 'wrap',
    marginBlockStart: 8,
  },
  hint: {fontSize: '0.72rem', color: colors.textDim},
  meta: {marginBlock: '6px 0', fontSize: '0.72rem', color: colors.textDim},
});
