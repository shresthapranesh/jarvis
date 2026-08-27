import * as stylex from '@stylexjs/stylex';

import {channels, colors, type} from '../theme/tokens.stylex';

/* ── Styles for the memory-shaped screens ──────────────────────────────
   MemoryView, SkillsView and ProjectDetail all render the same shape: a
   page header, then sections of cards you can hover to reveal edit and
   delete on. The card and the reveal live here because all three share
   them; the page scaffolding itself is `page` in `./ui`. */

/** A card in a list — one memory item, one skill. */
export const item = stylex.create({
  root: {
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    gap: 12,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.borderStrong},
    borderRadius: 10,
    paddingBlock: 10,
    paddingInline: 14,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    transition: 'border-color 0.14s ease, background 0.14s ease',
    // The actions cluster rests at opacity 0 and comes up with the card. A
    // child cannot see its parent's :hover, so the card publishes it.
    '--item-actions-opacity': {
      default: '0',
      ':hover': '1',
      ':focus-within': '1',
      // Without a pointer there is no hover, so the actions would be
      // unreachable — on touch they are simply always visible.
      '@media (hover: none)': '1',
    },
  },
  main: {flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column', gap: 4},
  text: {
    fontSize: '0.88rem',
    lineHeight: 1.5,
    color: colors.text,
    whiteSpace: 'pre-wrap',
    overflowWrap: 'anywhere',
  },
  meta: {fontSize: '0.7rem', color: colors.textFaint},
  actions: {
    display: 'flex',
    gap: 4,
    flexShrink: 0,
    opacity: 'var(--item-actions-opacity)',
    transition: 'opacity 0.14s ease',
  },
});

/** The coloured dot that marks a memory's kind. */
export const kindDot = stylex.create({
  base: {width: 7, height: 7, borderRadius: '50%', flexShrink: 0, display: 'inline-block'},
  core: {
    backgroundColor: colors.signalInsight,
    boxShadow: `0 0 6px rgba(${channels.signalInsight}, 0.5)`,
  },
  fact: {
    backgroundColor: colors.accent,
    boxShadow: `0 0 6px rgba(${channels.accent}, 0.5)`,
  },
});

export function kindDotStyle(kind: string) {
  return kind === 'core' ? kindDot.core : kindDot.fact;
}

export const memory = stylex.create({
  sectionEmpty: {
    fontSize: '0.82rem',
    color: colors.textFaint,
    margin: 0,
    paddingBlock: 6,
    paddingInline: 2,
  },
  /** The kind shown as a read-only line, when editing can't change it. */
  kindStatic: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 7,
    fontSize: '0.82rem',
    color: colors.textDim,
    paddingBlock: 6,
    paddingInline: 2,
  },
  subtitleCode: {
    fontSize: '0.78rem',
    backgroundColor: colors.surface2,
    paddingBlock: 1,
    paddingInline: 5,
    borderRadius: 3,
  },
  metaLine: {
    fontSize: '0.78rem',
    color: colors.textDim,
    whiteSpace: 'nowrap',
    flexShrink: 0,
    paddingBlockStart: 6,
  },
});

/** The two-up segmented control that picks a memory's kind. */
export const seg = stylex.create({
  root: {display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8},
  opt: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'flex-start',
    gap: 3,
    paddingBlock: 9,
    paddingInline: 12,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.borderStrong},
    borderRadius: 9,
    color: colors.textDim,
    fontFamily: 'inherit',
    cursor: 'pointer',
    textAlign: 'left',
    transition: 'border-color 0.14s ease, background 0.14s ease',
    // Read by `hint` below — the hint brightens with its own option.
    '--seg-hint-color': colors.textFaint,
  },
  optActive: {
    backgroundColor: {default: colors.accentDim, ':hover': colors.accentDim},
    borderColor: {default: colors.accent, ':hover': colors.accent},
    color: colors.text,
    '--seg-hint-color': colors.textDim,
  },
  label: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 7,
    fontSize: '0.84rem',
    fontWeight: 600,
  },
  hint: {fontSize: '0.72rem', color: 'var(--seg-hint-color)'},
});

/** A skill card — the same shell as `item`, laid out as a column. */
export const skill = stylex.create({
  card: {
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.borderStrong},
    borderRadius: 10,
    paddingBlock: 12,
    paddingInline: 14,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    transition: 'border-color 0.14s ease, background 0.14s ease, opacity 0.14s ease',
    '--item-actions-opacity': {
      default: '0',
      ':hover': '1',
      ':focus-within': '1',
      '@media (hover: none)': '1',
    },
  },
  cardDisabled: {opacity: 0.55},
  head: {display: 'flex', alignItems: 'center', gap: 10},
  /** Settings' model cards reuse the head, left-aligned and tighter. */
  headStart: {justifyContent: 'flex-start', gap: 6},
  name: {
    fontFamily: type.mono,
    fontSize: '0.86rem',
    fontWeight: 600,
    color: colors.text,
    overflowWrap: 'anywhere',
  },
  controls: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    marginInlineStart: 'auto',
    flexShrink: 0,
  },
  desc: {fontSize: '0.82rem', lineHeight: 1.5, color: colors.textDim, margin: 0},

  body: {fontSize: '0.78rem'},
  summary: {
    cursor: 'pointer',
    color: {default: colors.textFaint, ':hover': colors.accent},
    userSelect: 'none',
    width: 'fit-content',
    transition: 'color 0.12s ease',
  },
  pre: {
    marginBlock: '8px 2px',
    paddingBlock: 10,
    paddingInline: 12,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 8,
    fontFamily: type.mono,
    fontSize: '0.75rem',
    lineHeight: 1.55,
    color: colors.textDim,
    whiteSpace: 'pre-wrap',
    overflowWrap: 'anywhere',
    maxHeight: 320,
    overflowY: 'auto',
  },
  /** Name and body are code, so the editor fields go mono. */
  monoField: {fontFamily: type.mono, fontSize: '0.8rem'},
});
