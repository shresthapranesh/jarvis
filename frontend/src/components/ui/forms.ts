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
    fontSize: type.tMicro,
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
  },
  input: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 2,
    color: colors.text,
    fontSize: type.tBody,
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
    borderRadius: 2,
    color: colors.text,
    fontSize: type.tBody,
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
    borderRadius: 2,
    color: colors.text,
    fontSize: type.tBody,
    fontFamily: 'inherit',
    paddingBlock: 8,
    paddingInline: 10,
    outline: 'none',
    transition: 'border-color 0.15s',
    cursor: 'pointer',
  },
  /**
   * Native `<select>` chrome, removed and redrawn.
   *
   * A select ignores background, border and radius until `appearance` is
   * cleared — the OS draws its own rounded widget over whatever the stylesheet
   * said, which is why every select in the app rendered as a system control on
   * a themed page. Clearing it also removes the arrow, so the caret is drawn
   * here as two gradient halves: a background-image can hold a design token,
   * and a data-URI SVG cannot (`currentColor` does not cross that boundary),
   * so this is the only spelling of the arrow that follows the theme.
   *
   * Deliberately separate from `select` above: several call sites share one
   * base style between their inputs and their selects, so this is applied as a
   * second argument at each `<select>` and the caret lands only there. It also
   * has to come second for `paddingInlineEnd` to win over the base's
   * `paddingInline`.
   */
  selectChrome: {
    appearance: 'none',
    WebkitAppearance: 'none',
    backgroundImage: `linear-gradient(45deg, transparent 50%, ${colors.textDim} 50%), linear-gradient(135deg, ${colors.textDim} 50%, transparent 50%)`,
    backgroundPosition: 'right 14px top 53%, right 9.5px top 53%',
    backgroundSize: '5px 5px, 5px 5px',
    backgroundRepeat: 'no-repeat',
    paddingInlineEnd: 28,
  },
  hint: {fontSize: type.tUi, color: colors.textDim, marginBlockStart: 4},
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
  key: {fontFamily: type.mono, fontSize: type.tSmall, color: colors.textDim},
  input: {
    width: '100%',
    paddingBlock: 6,
    paddingInline: 9,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 2,
    backgroundColor: colors.surface,
    color: colors.text,
    fontFamily: type.mono,
    fontSize: type.tUi,
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
    borderRadius: 2,
    backgroundColor: colors.surface,
    color: colors.textDim,
    fontFamily: type.mono,
    fontSize: type.tSmall,
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
  hint: {fontSize: type.tSmall, color: colors.textDim},
  meta: {marginBlock: '6px 0', fontSize: type.tSmall, color: colors.textDim},
});
