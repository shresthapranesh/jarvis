/* ════════════════════════════════════════════════════════════════════
   Page scaffolding and status badges — the shape almost every screen takes.
   ════════════════════════════════════════════════════════════════════ */
import * as stylex from '@stylexjs/stylex';

import {channels, colors, type} from '../../theme/tokens.stylex';

/**
 * Page scaffolding. `page.root` is the flex column every route mounts as;
 * `page.scroll` is the padded, scrolling variant most settings-like screens
 * use directly.
 */
export const page = stylex.create({
  root: {display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden'},
  scroll: {
    // 40px of a 390px screen is a tenth of the content, so the gutter closes
    // up on a phone. This used to be a separate `.memory-page, .tasks-page,
    // .artifacts-loading` rule 1,900 lines further down the stylesheet.
    paddingBlockStart: {default: 32, '@media (max-width: 768px)': 20},
    paddingBlockEnd: 32,
    paddingInline: {default: 40, '@media (max-width: 768px)': 16},
    overflowY: 'auto',
    height: '100%',
    display: 'flex',
    flexDirection: 'column',
    gap: 16,
  },
  header: {
    display: 'flex',
    alignItems: 'flex-start',
    justifyContent: 'space-between',
    // `headerActions` never shrinks, which on a phone strands the title and
    // subtitle in a ~130px column. Let the actions take their own row.
    flexWrap: {default: null, '@media (max-width: 768px)': 'wrap'},
    gap: {default: 24, '@media (max-width: 768px)': 12},
    marginBlockEnd: 4,
  },
  headerMain: {flex: '1 1 100%', minWidth: 0},
  headerActions: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    flexShrink: 0,
    paddingBlockStart: 4,
  },
  title: {fontSize: type.tPage, fontWeight: 600, marginBlock: '0 4px', color: colors.text},
  subtitle: {
    fontSize: type.tBody,
    color: colors.textDim,
    maxWidth: 640,
    lineHeight: 1.5,
    margin: 0,
  },
  section: {display: 'flex', flexDirection: 'column', gap: 8},
  sectionTitle: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    fontSize: type.tBody,
    fontWeight: 600,
    color: colors.text,
    marginBlock: '8px 0',
  },
  sectionHint: {
    fontSize: type.tSmall,
    fontWeight: 400,
    color: colors.textFaint,
    marginInlineStart: 2,
  },
  list: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 6,
  },
  count: {
    fontSize: type.tSmall,
    fontWeight: 500,
    color: colors.textDim,
    backgroundColor: colors.surface2,
    borderRadius: 3,
    paddingBlock: 1,
    paddingInline: 8,
  },
  itemMeta: {fontSize: type.tMicro, color: colors.textFaint},
  empty: {
    paddingBlock: 40,
    paddingInline: 16,
    textAlign: 'center',
    color: colors.textDim,
    fontSize: type.tBody,
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.border,
    borderRadius: 3,
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
    alignItems: 'center',
  },
  error: {
    paddingBlock: 8,
    paddingInline: 12,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.errorText,
    borderRadius: 2,
    color: colors.errorText,
    fontSize: type.tUi,
    backgroundColor: `rgba(${channels.danger}, 0.08)`,
  },
});

export const badge = stylex.create({
  base: {
    display: 'inline-flex',
    alignItems: 'center',
    fontSize: type.tMicro,
    fontWeight: 500,
    paddingBlock: 1,
    paddingInline: 8,
    borderRadius: 2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    backgroundColor: colors.surface2,
    color: colors.textDim,
    whiteSpace: 'nowrap',
  },
  live: {
    backgroundColor: colors.signalLiveDim,
    color: colors.signalLive,
    borderColor: `rgba(${channels.signalLive}, 0.22)`,
  },
  warn: {
    backgroundColor: `rgba(${channels.warn}, 0.12)`,
    color: colors.warn,
    borderColor: `rgba(${channels.warn}, 0.24)`,
  },
  stdio: {
    backgroundColor: `rgba(${channels.signalTool}, 0.12)`,
    color: colors.signalTool,
    borderColor: `rgba(${channels.signalTool}, 0.2)`,
  },
  http: {
    backgroundColor: 'rgba(96, 165, 250, 0.12)',
    color: '#60a5fa',
    borderColor: 'rgba(96, 165, 250, 0.2)',
  },
  sse: {
    backgroundColor: `rgba(${channels.signalInsight}, 0.12)`,
    color: colors.signalInsight,
    borderColor: `rgba(${channels.signalInsight}, 0.2)`,
  },
});

/**
 * Live-run ornaments: the blinking caret on a streaming reply, the bouncing
 * dots while a turn has no text yet, and the small green "in progress" dot.
 * Shared by the chat thread, the activity sidebar, the automation run panel
 * and live mode — all four render the same run, in different frames.
 */

/**
 * The run-kind pill on the Tasks and Approvals pages. Kind is server data
 * (`chat` | `automation` | `workflow` | `board_task`), so it is looked up
 * rather than typed as a variant set.
 */
export const kindBadge = stylex.create({
  base: {
    display: 'inline-flex',
    alignItems: 'center',
    fontSize: type.tMicro,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
    paddingBlock: 3,
    paddingInline: 8,
    borderRadius: 2,
    flexShrink: 0,
  },
  chat: {backgroundColor: colors.accentDim, color: colors.accent},
  automation: {backgroundColor: 'rgba(251, 191, 119, 0.15)', color: colors.warningText},
  workflow: {backgroundColor: colors.webhookBg, color: colors.webhookText},
  // Board is the one durable source, so it gets its own hue rather than
  // borrowing a run kind's.
  board_task: {backgroundColor: `rgba(${channels.ok}, 0.16)`, color: colors.ok},
});

export function kindBadgeStyle(kind: string) {
  if (kind === 'chat') return kindBadge.chat;
  if (kind === 'automation') return kindBadge.automation;
  if (kind === 'workflow') return kindBadge.workflow;
  if (kind === 'board_task') return kindBadge.board_task;
  return null;
}
