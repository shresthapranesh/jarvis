import * as stylex from '@stylexjs/stylex';
import {createFileRoute, useNavigate} from '@tanstack/react-router';

import {kindBadge, kindBadgeStyle, page} from '../components/ui';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {
  refreshRunningTasks,
  useRunningTasks,
  useRunningTasksLoaded,
} from '../hooks/useRunningTasks';
import {formatRelativeTime} from '../lib/api';
import type {RunningTask, TaskKind} from '../lib/types';
import {commitStopRunningTask} from '../relay/StopRunningTaskMutation';
import {channels, colors, type} from '../theme/tokens.stylex';

export const Route = createFileRoute('/tasks')({
  component: TasksPage,
});

const KIND_LABEL: Record<TaskKind, string> = {
  chat: 'Chat',
  automation: 'Automation',
  workflow: 'Workflow',
};

function TasksPage() {
  const navigate = useNavigate();

  const tasks = useRunningTasks();
  const loaded = useRunningTasksLoaded();

  // Stopping is the one case the 2s poll would show late, so re-read at once.
  const stopAction = useAsyncAction((id: string) =>
    commitStopRunningTask(id).then(refreshRunningTasks),
  );

  function goTo(task: RunningTask) {
    if (!task.parent_id) return;
    if (task.kind === 'chat') {
      navigate({to: '/c/$id', params: {id: task.parent_id}});
    } else if (task.kind === 'automation') {
      navigate({to: '/automation'});
    } else if (task.kind === 'workflow') {
      navigate({to: '/workflow/$id', params: {id: task.parent_id}});
    }
  }

  return (
    <div {...stylex.props(page.scroll, styles.page)}>
      <header {...stylex.props(styles.header)}>
        <h1 {...stylex.props(page.title)}>Tasks</h1>
        <p {...stylex.props(page.subtitle, styles.subtitle)}>
          Currently running across chat, automations, and workflows. Tasks disappear when they
          finish or stop.
        </p>
      </header>

      {!loaded ? (
        <div {...stylex.props(page.empty)}>Loading…</div>
      ) : tasks.length === 0 ? (
        <div {...stylex.props(page.empty)}>No active tasks.</div>
      ) : (
        <ul {...stylex.props(page.list, styles.list)}>
          {tasks.map((task) => {
            const pct = task.total_tokens
              ? Math.min(100, Math.round(((task.total_tokens || 0) / 500000) * 100))
              : 0;
            return (
              <li key={task.id} {...stylex.props(styles.row)}>
                <div {...stylex.props(styles.rowBody)}>
                  <button
                    {...stylex.props(styles.rowMain)}
                    type="button"
                    onClick={() => goTo(task)}
                    disabled={!task.parent_id}
                  >
                    <span {...stylex.props(kindBadge.base, kindBadgeStyle(task.kind))}>
                      {KIND_LABEL[task.kind]}
                    </span>
                    <span {...stylex.props(styles.label)}>{task.label || task.id}</span>
                    <span {...stylex.props(styles.elapsed)}>
                      started {formatRelativeTime(task.started_at)}
                    </span>
                    {task.has_interrupt && (
                      <span {...stylex.props(styles.flag, styles.flagInterrupt)}>
                        awaiting input
                      </span>
                    )}
                    {task.cancelled && (
                      <span {...stylex.props(styles.flag, styles.flagCancelling)}>stopping…</span>
                    )}
                    {task.budget_exceeded && (
                      <span {...stylex.props(styles.flag, styles.flagCancelling)}>
                        budget exceeded
                      </span>
                    )}
                  </button>
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
  // The header carries its own bottom margin, so the flex gap is off.
  page: {gap: 0},
  header: {marginBlockEnd: 24},
  subtitle: {maxWidth: 540},
  list: {gap: 8},

  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    paddingBlock: 4,
    paddingInlineStart: 12,
    paddingInlineEnd: 6,
    transition: 'background 0.12s, border-color 0.12s',
  },
  rowBody: {flex: 1, display: 'flex', flexDirection: 'column', gap: 4},
  rowMain: {
    flex: 1,
    display: 'flex',
    alignItems: 'center',
    gap: 12,
    backgroundColor: 'transparent',
    borderWidth: 0,
    borderStyle: 'none',
    color: colors.text,
    fontSize: type.tBody,
    fontFamily: 'inherit',
    textAlign: 'left',
    paddingBlock: 8,
    paddingInline: 4,
    cursor: {default: 'pointer', ':disabled': 'default'},
  },
  label: {
    fontWeight: 500,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    flex: 1,
  },
  elapsed: {fontSize: type.tUi, color: colors.textDim, flexShrink: 0},

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
    borderColor: colors.errorBorder,
    borderRadius: 2,
    color: colors.errorText,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.5},
    flexShrink: 0,
    filter: {default: null, ':hover:not(:disabled)': 'brightness(1.15)'},
    transition: 'filter 0.12s',
  },
});
