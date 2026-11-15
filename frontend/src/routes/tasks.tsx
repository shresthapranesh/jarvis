import * as stylex from '@stylexjs/stylex';
import {createFileRoute, Link} from '@tanstack/react-router';

import {kindBadge, kindBadgeStyle, page, PageHeader, TimeAgo} from '../components/ui';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {
  refreshRunningTasks,
  useRunningTasks,
  useRunningTasksLoaded,
} from '../hooks/useRunningTasks';
import {runLink} from '../lib/runLink';
import type {TaskKind} from '../lib/types';
import {commitStopRunningTask} from '../relay/StopRunningTaskMutation';
import {bp, channels, colors, type} from '../theme/tokens.stylex';

export const Route = createFileRoute('/tasks')({
  component: TasksPage,
});

const KIND_LABEL: Record<TaskKind, string> = {
  chat: 'Chat',
  automation: 'Automation',
  workflow: 'Workflow',
  board_task: 'Board task',
};

function TasksPage() {
  const tasks = useRunningTasks();
  const loaded = useRunningTasksLoaded();

  // Stopping is the one case the 2s poll would show late, so re-read at once.
  const stopAction = useAsyncAction((id: string) =>
    commitStopRunningTask(id).then(refreshRunningTasks),
  );

  return (
    <div {...stylex.props(page.scroll)}>
      <PageHeader
        title="Running"
        subtitle="Everything running right now, across chats, automations and workflows."
      />

      {!loaded ? (
        <div {...stylex.props(page.empty)}>Loading…</div>
      ) : tasks.length === 0 ? (
        <div {...stylex.props(page.empty)}>
          <p>Nothing is running.</p>
          <p>Chats, automations and workflows show up here while they work, with their progress.</p>
        </div>
      ) : (
        <ul {...stylex.props(page.list, styles.list)}>
          {tasks.map((task) => {
            const pct = task.total_tokens
              ? Math.min(100, Math.round(((task.total_tokens || 0) / 500000) * 100))
              : 0;
            const link = runLink(task);
            const main = (
              <>
                <span {...stylex.props(kindBadge.base, kindBadgeStyle(task.kind))}>
                  {KIND_LABEL[task.kind] ?? task.kind}
                </span>
                <span {...stylex.props(styles.label)}>{task.label || task.id}</span>
                <span {...stylex.props(styles.elapsed)}>
                  started <TimeAgo iso={task.started_at} />
                </span>
                {task.has_interrupt && (
                  <span {...stylex.props(styles.flag, styles.flagInterrupt)}>awaiting input</span>
                )}
                {task.cancelled && (
                  <span {...stylex.props(styles.flag, styles.flagCancelling)}>stopping…</span>
                )}
                {task.budget_exceeded && (
                  <span {...stylex.props(styles.flag, styles.flagCancelling)}>budget exceeded</span>
                )}
              </>
            );
            return (
              <li key={task.id} {...stylex.props(styles.row)}>
                <div {...stylex.props(styles.rowBody)}>
                  {link ? (
                    <Link {...link} {...stylex.props(styles.rowMain, styles.rowMainLink)}>
                      {main}
                    </Link>
                  ) : (
                    <div {...stylex.props(styles.rowMain)}>{main}</div>
                  )}
                  <div {...stylex.props(styles.budget)}>
                    <span title={`${task.input_tokens} in / ${task.output_tokens} out`}>
                      {task.total_tokens?.toLocaleString() ?? 0} tokens · {task.llm_calls} llm ·{' '}
                      {task.tool_calls} tools
                    </span>
                    <div {...stylex.props(styles.bar)} title={`${pct}% of 500k default budget`}>
                      {/* Width is the live proportion, so it stays inline; the
                          colour is a threshold, so it is a style variant. */}
                      <div
                        {...stylex.props(
                          styles.barFill,
                          task.budget_exceeded
                            ? styles.barOver
                            : pct > 80
                              ? styles.barWarn
                              : styles.barOk,
                        )}
                        style={{width: `${pct}%`}}
                      />
                    </div>
                    {task.budget_reason && (
                      <span {...stylex.props(styles.budgetReason)}>{task.budget_reason}</span>
                    )}
                  </div>
                </div>
                <button
                  {...stylex.props(styles.stopBtn)}
                  type="button"
                  disabled={task.cancelled || stopAction.pending}
                  onClick={() => void stopAction.run(task.id)}
                >
                  Stop
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

const styles = stylex.create({
  list: {gap: 8},

  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: 'transparent',
    borderRadius: 3,
    paddingBlock: 4,
    paddingInlineStart: 12,
    paddingInlineEnd: 6,
    transition: 'background 0.12s',
  },
  rowBody: {flex: 1, display: 'flex', flexDirection: 'column', gap: 4},
  rowMain: {
    flex: 1,
    display: 'flex',
    alignItems: 'center',
    // On a phone the start time drops to its own line, so the name keeps the
    // width beside the badge instead of being cut to a few letters.
    flexWrap: {default: 'nowrap', [bp.compact]: 'wrap'},
    columnGap: 12,
    rowGap: 4,
    color: colors.text,
    fontSize: type.tBody,
    paddingBlock: 8,
    paddingInline: 4,
  },
  rowMainLink: {textDecoration: 'none', cursor: 'pointer'},
  label: {
    fontWeight: 500,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    flex: 1,
  },
  elapsed: {
    fontSize: type.tUi,
    color: colors.textDim,
    flexShrink: 0,
    flexBasis: {default: 'auto', [bp.compact]: '100%'},
  },

  flag: {fontSize: type.tMicro, paddingBlock: 2, paddingInline: 6, borderRadius: 2, flexShrink: 0},
  flagInterrupt: {backgroundColor: `rgba(${channels.accent}, 0.18)`, color: colors.accent},
  flagCancelling: {backgroundColor: colors.errorBg, color: colors.errorText},

  budget: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    paddingBlockEnd: 4,
    paddingInlineStart: 12,
    paddingInlineEnd: 4,
    fontSize: type.tSmall,
    color: colors.textDim,
  },
  bar: {
    flex: 1,
    maxWidth: 160,
    height: 4,
    backgroundColor: colors.surface2,
    borderRadius: 2,
    overflow: 'hidden',
  },
  barFill: {height: '100%', transition: 'width 0.3s'},
  barOk: {backgroundColor: colors.textDim},
  barWarn: {backgroundColor: colors.warningText},
  barOver: {backgroundColor: colors.errorText},
  budgetReason: {
    color: colors.errorText,
    maxWidth: 220,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },

  stopBtn: {
    fontSize: type.tUi,
    fontFamily: 'inherit',
    paddingBlock: 6,
    paddingInline: 14,
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: 'transparent',
    borderRadius: 2,
    color: colors.errorText,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.5},
    flexShrink: 0,
    filter: {default: null, ':hover:not(:disabled)': 'brightness(1.15)'},
    transition: 'filter 0.12s',
  },
});
