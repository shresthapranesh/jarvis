import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, css, layout, type} from '../theme/tokens.stylex';

/* ── Styles for the workflow screens ───────────────────────────────────
   Shared by WorkflowEditor, WorkflowEditorPage, WorkflowRunPage and the
   `/workflow` list route, which is why this module is not named after any
   one of them: the config controls, the modal and the node card are used
   by three of the four.

   What stays a global class, and why: `.wf-handle*` and the `.react-flow__*`
   overrides in base.css style DOM that @xyflow/react owns and renders
   itself. Those are the same documented exception as `[data-md]` — StyleX
   can only style an element it is handed. */

/** The `/workflow` index: a header, an optional create form, and rows. */
export const list = stylex.create({
  page: {display: 'flex', flexDirection: 'column', height: '100%', overflowY: 'auto'},
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBlockStart: 18,
    paddingBlockEnd: 14,
    paddingInline: 24,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  title: {fontSize: type.tBody, fontWeight: 600, color: colors.text, margin: 0},
  createForm: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: 12,
    paddingInline: 24,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    backgroundColor: colors.surface2,
    flexShrink: 0,
  },
  createInput: {flex: 1},
  empty: {
    paddingBlock: 40,
    paddingInline: 24,
    color: colors.textDim,
    fontSize: type.tUi,
    textAlign: 'center',
  },

  row: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 12,
    paddingBlock: 14,
    paddingInline: 24,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    backgroundColor: {default: null, ':hover': colors.surface2},
    transition: 'background 0.12s',
    cursor: 'pointer',
  },
  rowInfo: {display: 'flex', flexDirection: 'column', gap: 3, minWidth: 0},
  rowName: {fontSize: type.tBody, fontWeight: 600, color: colors.text},
  rowDesc: {
    fontSize: type.tUi,
    color: colors.textDim,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
    maxWidth: 480,
  },
  rowMeta: {fontSize: type.tMicro, color: colors.textDim, opacity: 0.7},
  rowActions: {display: 'flex', gap: 6, flexShrink: 0},
});

/** The gradient "+ New" / "Create" button the list header uses. */
export const newBtn = stylex.create({
  base: {
    backgroundImage: `linear-gradient(135deg, ${colors.accentStrong}, ${colors.accent})`,
    borderStyle: 'none',
    borderRadius: 3,
    color: colors.accentContrast,
    fontSize: type.tUi,
    fontFamily: 'inherit',
    paddingBlock: 6,
    paddingInline: 14,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.5},
    boxShadow: {
      default: `0 2px 10px rgba(${channels.accent}, 0.3)`,
      ':hover': `0 4px 16px rgba(${channels.accent}, 0.45)`,
    },
    transform: {default: null, ':hover': 'translateY(-1px)'},
    transition: 'box-shadow 0.2s, transform 0.15s',
  },
});

/** The two buttons that sit in the canvas toolbar and the top bar. */
export const wfBtn = stylex.create({
  save: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover:not(:disabled)': colors.accent},
    color: {default: colors.text, ':hover:not(:disabled)': colors.accent},
    borderRadius: 2,
    paddingBlock: 5,
    paddingInline: 14,
    fontSize: type.tUi,
    fontFamily: 'inherit',
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.5},
    transition: 'border-color 0.15s',
    whiteSpace: 'nowrap',
  },
  /** The History toggle while the sidebar is open. */
  saveActive: {
    backgroundColor: colors.accentDim,
    color: colors.accent,
    borderColor: colors.accent,
  },
  run: {
    backgroundColor: colors.accent,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.accent,
    color: colors.accentContrast,
    borderRadius: 2,
    paddingBlock: 5,
    paddingInline: 14,
    fontSize: type.tUi,
    fontFamily: 'inherit',
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    fontWeight: 600,
    opacity: {default: 1, ':disabled': 0.5},
    transition: 'opacity 0.15s',
    whiteSpace: 'nowrap',
  },
  back: {
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    borderStyle: 'none',
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
    fontSize: type.tUi,
    fontFamily: 'inherit',
    paddingBlock: 4,
    paddingInline: 6,
    borderRadius: 2,
    whiteSpace: 'nowrap',
  },
  /** Destructive. `inline` drops the auto top margin the panel version needs. */
  del: {
    marginBlockStart: 'auto',
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.errorBorder,
    color: colors.errorText,
    borderRadius: 2,
    paddingBlock: 6,
    paddingInline: 10,
    fontSize: type.tUi,
    fontFamily: 'inherit',
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':hover': 0.8, ':disabled': 0.5},
    transition: 'opacity 0.15s',
  },
  delInline: {marginBlockStart: 0},
});

/** A centred dialog — delete confirmation, run inputs. */
export const modal = stylex.create({
  backdrop: {
    position: 'fixed',
    inset: 0,
    backgroundColor: `rgba(${channels.shadow}, 0.55)`,
    zIndex: 100,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
  },
  root: {
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    paddingBlock: 22,
    paddingInline: 24,
    // The StyleX modals elsewhere already size as `calc(100% - 32px)`; this
    // one carries a min-width, so the phone case has to be spelled out.
    minWidth: {default: 340, '@media (max-width: 768px)': 0},
    width: {default: null, '@media (max-width: 768px)': 'calc(100% - 32px)'},
    maxWidth: 480,
    display: 'flex',
    flexDirection: 'column',
    gap: 14,
  },
  title: {fontSize: type.tBody, fontWeight: 600, color: colors.text},
  body: {fontSize: type.tUi, color: colors.textDim, margin: 0},
  strong: {color: colors.text},
  actions: {display: 'flex', justifyContent: 'flex-end', gap: 8, paddingBlockStart: 4},
});

/**
 * The status pill both the history rows and the live run panel show.
 * Status is server data, so `statusStyle()` looks the variant up and an
 * unrecognised one renders as the neutral base.
 */
export const runStatus = stylex.create({
  base: {
    fontSize: type.tMicro,
    fontWeight: 500,
    paddingBlock: 2,
    paddingInline: 7,
    borderRadius: 2,
    flexShrink: 0,
  },
  running: {
    backgroundColor: colors.accentDim,
    color: colors.accent,
    animationName: kf.pulse,
    animationDuration: '1.4s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  done: {backgroundColor: `rgba(${channels.ok}, 0.14)`, color: colors.ok},
  error: {backgroundColor: colors.errorBg, color: colors.errorText},
});

export function statusStyle(status: string) {
  if (status === 'running') return runStatus.running;
  if (status === 'done') return runStatus.done;
  if (status === 'error') return runStatus.error;
  return null;
}

/** The editor shell: top bar, then palette | canvas | config panel. */
export const editor = stylex.create({
  page: {display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden'},
  topbar: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    paddingInline: 16,
    height: 48,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.glassBorder,
    backgroundColor: colors.glassBg,
    backdropFilter: layout.blur,
    WebkitBackdropFilter: layout.blur,
    flexShrink: 0,
  },
  name: {
    fontSize: type.tBody,
    fontWeight: 600,
    flex: 1,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  body: {flex: 1, display: 'flex', overflow: 'hidden', minHeight: 0},
  inner: {
    display: 'flex',
    width: '100%',
    height: '100%',
    position: 'relative',
    overflow: 'hidden',
  },
  /**
   * The canvas also publishes the palette that base.css's `.react-flow__*`
   * and `.wf-handle` rules read. Those style DOM @xyflow/react renders
   * itself, so they stay global selectors — but their colours come from
   * here, which keeps them on the theme.
   */
  canvas: {
    flex: 1,
    height: '100%',
    position: 'relative',
    overflow: 'hidden',
    '--rf-bg': colors.bg,
    '--rf-surface': colors.surface,
    '--rf-surface2': colors.surface2,
    '--rf-border': colors.border,
    '--rf-text-dim': colors.textDim,
    '--rf-accent': colors.accent,
    '--rf-error-border': colors.errorBorder,
    '--rf-ok': colors.ok,
  },
  canvasToolbar: {
    position: 'absolute',
    insetBlockStart: 12,
    insetInlineEnd: 12,
    display: 'flex',
    gap: 6,
    zIndex: 10,
  },
});

/** The draggable node source list down the left edge of the canvas. */
export const palette = stylex.create({
  root: {
    width: 150,
    backgroundColor: colors.glassBg,
    backdropFilter: layout.blur,
    WebkitBackdropFilter: layout.blur,
    borderInlineEndWidth: 1,
    borderInlineEndStyle: 'solid',
    borderInlineEndColor: colors.glassBorder,
    paddingBlock: 10,
    paddingInline: 6,
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
    flexShrink: 0,
    overflowY: 'auto',
    scrollbarWidth: 'thin',
    scrollbarColor: `${colors.border} transparent`,
    zIndex: 1,
  },
  label: {
    fontSize: type.tNano,
    color: colors.textDim,
    textTransform: 'uppercase',
    letterSpacing: '0.08em',
    paddingInline: 4,
    paddingBlockEnd: 4,
  },
  hint: {
    fontSize: type.tNano,
    color: colors.textDim,
    padding: 4,
    opacity: 0.6,
    lineHeight: 1.4,
  },
  item: {
    backgroundColor: {default: colors.surface2, ':hover': colors.accentDim},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.accent},
    borderRadius: 2,
    paddingBlock: 6,
    paddingInline: 8,
    fontSize: type.tSmall,
    color: colors.text,
    cursor: 'grab',
    transition: 'border-color 0.15s, background 0.15s',
    userSelect: 'none',
  },
  itemHint: {fontSize: type.tNano, marginBlockStart: 1, opacity: 0.65},
});

/**
 * The card React Flow renders for each node.
 *
 * Only four types ever carried a top accent (`agent`, `conditional`, `start`,
 * `map`); the rest fell through to the plain card, and `accentFor` keeps that
 * rather than inventing colours for the eight that never had one.
 */
export const node = stylex.create({
  root: {
    backgroundColor: colors.glassBg,
    backdropFilter: 'blur(12px)',
    WebkitBackdropFilter: 'blur(12px)',
    borderWidth: 1.5,
    borderStyle: 'solid',
    borderColor: `rgba(${channels.tint}, 0.1)`,
    borderRadius: 3,
    minWidth: 160,
    maxWidth: 220,
    boxShadow: `0 4px 16px rgba(${channels.shadow}, 0.4)`,
    fontSize: type.tUi,
    color: colors.text,
    position: 'relative',
  },
  selected: {
    borderColor: colors.accent,
    boxShadow: `0 0 0 2px ${colors.accent}, 0 2px 8px rgba(${channels.shadow}, 0.4)`,
  },

  // Execution status. These beat `selected` because they come later in the
  // props() argument list, which is what the old `!important` was standing in
  // for — the two rules had equal specificity and source order decided it.
  running: {
    boxShadow: `0 0 0 2px ${colors.accent}, 0 2px 8px rgba(${channels.shadow}, 0.3)`,
    animationName: kf.wfPulse,
    animationDuration: '1.4s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  done: {boxShadow: `0 0 0 2px ${colors.ok}, 0 2px 8px rgba(${channels.shadow}, 0.3)`},
  error: {
    boxShadow: `0 0 0 2px ${colors.errorBorder}, 0 2px 8px rgba(${channels.shadow}, 0.3)`,
  },

  accentAgent: {borderBlockStartWidth: 2, borderBlockStartColor: colors.accent},
  accentCond: {borderBlockStartWidth: 2, borderBlockStartColor: colors.warn},
  accentStart: {borderBlockStartWidth: 2, borderBlockStartColor: colors.signalInsight},
  accentMap: {borderBlockStartWidth: 2, borderBlockStartColor: colors.signalTool},

  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    paddingBlockStart: 8,
    paddingBlockEnd: 6,
    paddingInline: 10,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
  },
  label: {
    fontWeight: 600,
    fontSize: type.tUi,
    flex: 1,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  preview: {
    paddingBlockStart: 6,
    paddingBlockEnd: 8,
    paddingInline: 10,
    fontSize: type.tMicro,
    color: colors.textDim,
    lineHeight: 1.4,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  mapProgress: {opacity: 0.75, fontSize: '0.75em'},

  badge: {
    fontSize: type.tNano,
    textTransform: 'uppercase',
    letterSpacing: '0.06em',
    backgroundColor: colors.accentDim,
    color: colors.accent,
    borderRadius: 2,
    paddingBlock: 1,
    paddingInline: 5,
    fontWeight: 600,
  },
  badgeCond: {backgroundColor: `rgba(${channels.warn}, 0.15)`, color: colors.warn},
  badgeStart: {backgroundColor: colors.signalInsightDim, color: colors.signalInsight},
  badgeMap: {backgroundColor: colors.signalToolDim, color: colors.signalTool},

  /** The true/false caption under a branching node's two output handles. */
  condLabel: {
    position: 'absolute',
    insetBlockEnd: -16,
    fontSize: type.tNano,
    color: colors.textDim,
    pointerEvents: 'none',
    transform: 'translateX(-50%)',
  },
  condTrue: {insetInlineStart: '30%'},
  condFalse: {insetInlineStart: '70%'},
});

/** The right-hand panel that edits the selected node. */
export const config = stylex.create({
  panel: {
    width: 260,
    backgroundColor: colors.glassBg,
    backdropFilter: layout.blur,
    WebkitBackdropFilter: layout.blur,
    borderInlineStartWidth: 1,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.glassBorder,
    paddingBlockStart: 14,
    paddingBlockEnd: 20,
    paddingInline: 14,
    display: 'flex',
    flexDirection: 'column',
    gap: 12,
    overflowY: 'auto',
    flexShrink: 0,
    zIndex: 1,
  },
  panelTitle: {
    fontSize: type.tSmall,
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
    paddingBlockEnd: 6,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
  },
  field: {display: 'flex', flexDirection: 'column', gap: 4},
  label: {fontSize: type.tMicro, color: colors.textDim},
  hint: {
    fontSize: type.tMicro,
    color: colors.textDim,
    marginBlockEnd: 4,
    lineHeight: 1.4,
  },

  // One treatment for input / select / textarea; `textarea` adds the rest.
  input: {
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':focus': colors.accent},
    borderRadius: 2,
    color: colors.text,
    fontSize: type.tUi,
    paddingBlock: 6,
    paddingInline: 8,
    width: '100%',
    outline: 'none',
    fontFamily: 'inherit',
    transition: 'border-color 0.15s',
    boxSizing: 'border-box',
  },
  textarea: {
    resize: 'vertical',
    minHeight: 72,
    fontFamily: type.mono,
    fontSize: type.tSmall,
  },
  checkbox: {fontSize: type.tMicro, color: colors.textDim},

  section: {display: 'flex', flexDirection: 'column', gap: 8},
  sectionToggle: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    color: {default: colors.textDim, ':hover': colors.text},
    fontFamily: 'inherit',
    fontSize: type.tSmall,
    textAlign: 'left',
    padding: 0,
    cursor: 'pointer',
  },

  /** The map node's saved-workflow / inline-graph segmented control. */
  modeToggle: {
    display: 'flex',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    overflow: 'hidden',
    marginBlockStart: 4,
  },
  modeBtn: {
    flex: 1,
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    borderStyle: 'none',
    paddingBlock: 5,
    paddingInline: 0,
    fontSize: type.tSmall,
    fontFamily: 'inherit',
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
    transition: 'background 0.15s, color 0.15s',
    // The old rule was `.wf-map-mode-btn + .wf-map-mode-btn`; there are exactly
    // two buttons, so the divider is the second one's leading border.
    ':not(:first-child)': {
      borderInlineStartWidth: 1,
      borderInlineStartStyle: 'solid',
      borderInlineStartColor: colors.border,
    },
  },
  modeBtnActive: {
    backgroundColor: {default: colors.accentDim, ':hover': colors.accentDim},
    color: colors.text,
    fontWeight: 600,
  },
});

/** The live run tail that opens under the canvas while a run streams. */
export const runPanel = stylex.create({
  root: {
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    backgroundColor: colors.surface,
    maxHeight: 220,
    overflowY: 'auto',
    flexShrink: 0,
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBlock: 8,
    paddingInline: 16,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    fontSize: type.tUi,
    position: 'sticky',
    insetBlockStart: 0,
    backgroundColor: colors.surface,
    zIndex: 1,
  },
  statusRow: {
    paddingBlock: 6,
    paddingInline: 16,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    fontSize: type.tSmall,
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
  },
  statusHead: {display: 'flex', alignItems: 'center', gap: 8, color: colors.textDim},
  tokens: {
    color: colors.text,
    lineHeight: 1.5,
    maxHeight: 60,
    overflowY: 'auto',
    whiteSpace: 'pre-wrap',
    fontSize: type.tSmall,
    marginBlockStart: 3,
    fontFamily: type.mono,
  },
  outputs: {
    paddingBlock: 8,
    paddingInline: 16,
    fontSize: type.tSmall,
    fontFamily: type.mono,
    whiteSpace: 'pre-wrap',
    color: colors.textDim,
  },
});

/** The run-history sidebar, and the per-node records inside each row. */
export const history = stylex.create({
  sidebar: {
    width: {default: 340, '@media (max-width: 768px)': '100%'},
    maxWidth: {default: null, '@media (max-width: 768px)': '100%'},
    minWidth: {default: null, '@media (max-width: 768px)': 0},
    flexShrink: 0,
    borderInlineStartWidth: {default: 1, '@media (max-width: 768px)': 0},
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.border,
    paddingBlockStart: {default: null, '@media (max-width: 768px)': css.safeTop},
    paddingBlockEnd: {default: null, '@media (max-width: 768px)': css.safeBottom},
    backgroundColor: colors.surface,
    display: 'flex',
    flexDirection: 'column',
    overflow: 'hidden',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBlock: 16,
    paddingInline: 20,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  close: {
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    borderStyle: 'none',
    color: colors.textDim,
    cursor: 'pointer',
    fontSize: type.tBody,
    fontFamily: 'inherit',
    paddingBlock: 2,
    paddingInline: 6,
    borderRadius: 2,
  },
  empty: {
    paddingBlock: 32,
    paddingInline: 20,
    textAlign: 'center',
    color: colors.textDim,
    fontSize: type.tUi,
  },
  scroll: {flex: 1, overflowY: 'auto'},

  row: {
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
  },
  rowHeader: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    paddingBlock: 10,
    paddingInline: 20,
    cursor: 'pointer',
    backgroundColor: {default: null, ':hover': colors.surface2},
    transition: 'background 0.12s',
  },
  time: {fontSize: type.tUi, color: colors.textDim, flex: 1},
  duration: {fontSize: type.tSmall, color: colors.textDim, fontFamily: type.mono},
  chevron: {fontSize: type.tNano, color: colors.textDim},
  nodeCount: {fontSize: type.tMicro, color: colors.textDim},

  detail: {paddingBlockStart: 6, paddingBlockEnd: 12, paddingInline: 14},
  sectionLabel: {
    fontSize: type.tNano,
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
    fontWeight: 600,
    paddingBlockStart: 10,
    paddingBlockEnd: 5,
  },
  json: {
    fontSize: type.tSmall,
    fontFamily: type.mono,
    whiteSpace: 'pre-wrap',
    color: colors.textDim,
    margin: 0,
    lineHeight: 1.5,
  },
  error: {fontSize: type.tUi, color: colors.errorText, marginBlockEnd: 6},

  badge: {
    fontSize: type.tNano,
    paddingBlock: 1,
    paddingInline: 5,
    borderRadius: 2,
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
    fontWeight: 600,
    flexShrink: 0,
  },
  badgeAgent: {backgroundColor: 'rgba(79, 156, 249, 0.15)', color: colors.accent},
  badgeCond: {backgroundColor: `rgba(${channels.warn}, 0.12)`, color: colors.warn},
  badgeStart: {backgroundColor: 'rgba(139, 92, 246, 0.15)', color: colors.signalInsight},

  verdict: {
    fontSize: type.tNano,
    paddingBlock: 1,
    paddingInline: 6,
    borderRadius: 2,
    fontWeight: 600,
    flexShrink: 0,
  },
  verdictTrue: {backgroundColor: `rgba(${channels.ok}, 0.14)`, color: colors.ok},
  verdictFalse: {backgroundColor: `rgba(${channels.danger}, 0.14)`, color: colors.danger},
});

/**
 * The node-type badge. Only three types were ever given a colour; the rest
 * render as bare text, which is what the original stylesheet did.
 */
export function typeBadgeStyle(nodeType: string) {
  if (nodeType === 'agent') return history.badgeAgent;
  if (nodeType === 'conditional') return history.badgeCond;
  if (nodeType === 'start') return history.badgeStart;
  return null;
}

/** One node's record inside a history row or the run-detail panel. */
export const record = stylex.create({
  root: {
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    paddingBlock: 8,
    paddingInline: 10,
    marginBlockEnd: 5,
  },
  header: {display: 'flex', alignItems: 'center', gap: 7},
  label: {
    fontSize: type.tUi,
    color: colors.text,
    flex: 1,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
  duration: {
    fontSize: type.tMicro,
    color: colors.textDim,
    fontFamily: type.mono,
    flexShrink: 0,
  },
  error: {fontSize: type.tSmall, color: colors.errorText, marginBlockStart: 5},
  output: {
    fontSize: type.tMicro,
    fontFamily: type.mono,
    whiteSpace: 'pre-wrap',
    color: colors.textDim,
    marginBlockStart: 6,
    lineHeight: 1.5,
    maxHeight: 100,
    overflowY: 'auto',
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    paddingBlockStart: 5,
  },
  io: {
    marginBlockStart: 5,
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
    paddingBlockStart: 4,
  },
  ioLabel: {
    fontSize: type.tNano,
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
    fontWeight: 600,
    marginBlockEnd: 2,
  },
  pre: {
    fontSize: type.tMicro,
    fontFamily: type.mono,
    whiteSpace: 'pre-wrap',
    color: colors.textDim,
    lineHeight: 1.5,
    maxHeight: 120,
    overflowY: 'auto',
  },
  ioEmpty: {fontSize: type.tSmall, color: colors.textDim},
});

/** The right-hand panel on the run-detail page. */
export const runDetail = stylex.create({
  panel: {
    width: 360,
    flexShrink: 0,
    borderInlineStartWidth: 1,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.border,
    backgroundColor: colors.surface,
    display: 'flex',
    flexDirection: 'column',
    overflowY: 'auto',
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    paddingBlock: 14,
    paddingInline: 16,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  section: {paddingBlockStart: 10, paddingInline: 16},
  label: {
    fontSize: type.tNano,
    textTransform: 'uppercase',
    letterSpacing: '0.07em',
    color: colors.textDim,
    fontWeight: 600,
    marginBlockEnd: 4,
  },
  pre: {
    fontSize: type.tSmall,
    fontFamily: type.mono,
    whiteSpace: 'pre-wrap',
    color: colors.textDim,
    lineHeight: 1.5,
    maxHeight: 200,
    overflowY: 'auto',
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    paddingBlock: 6,
    paddingInline: 8,
    marginBlockEnd: 8,
  },
  error: {paddingBlock: 8, paddingInline: 16, fontSize: type.tUi, color: colors.errorText},
  empty: {
    flex: 1,
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: type.tUi,
    color: colors.textDim,
    borderInlineStartWidth: 1,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.border,
    padding: 16,
    width: 220,
    flexShrink: 0,
    textAlign: 'center',
  },
  summaryRow: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    paddingBlock: 7,
    paddingInline: 12,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexWrap: 'wrap',
  },
  summaryLabel: {
    fontSize: type.tUi,
    color: colors.text,
    flex: 1,
    minWidth: 0,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
});
