import {createFileRoute, useNavigate} from '@tanstack/react-router';

import {useAsyncAction} from '../hooks/useAsyncAction';
import {
  refreshRunningTasks,
  useRunningTasks,
  useRunningTasksLoaded,
} from '../hooks/useRunningTasks';
import {formatRelativeTime} from '../lib/api';
import {commitStopRunningTask} from '../relay/StopRunningTaskMutation';
import type {RunningTask, TaskKind} from '../lib/types';

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
  const stopAction = useAsyncAction(
    (id: string) => commitStopRunningTask(id).then(refreshRunningTasks),
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
    <div className="page tasks-page">
      <header className="tasks-header">
        <h1>Tasks</h1>
        <p className="tasks-subtitle">
          Currently running across chat, automations, and workflows. Tasks
          disappear when they finish or stop.
        </p>
      </header>

      {!loaded ? (
        <div className="tasks-empty">Loading…</div>
      ) : tasks.length === 0 ? (
        <div className="tasks-empty">No active tasks.</div>
      ) : (
        <ul className="tasks-list">
          {tasks.map((task) => {
            const pct = task.total_tokens ? Math.min(100, Math.round(((task.total_tokens || 0) / 500000) * 100)) : 0;
            const budgetColor = (task.budget_exceeded ? 'var(--error-text)' : pct > 80 ? 'var(--warning-text)' : 'var(--text-dim)');
            return (
            <li key={task.id} className="task-row">
              <div style={{flex:1, display:'flex', flexDirection:'column', gap:4}}>
                <button
                  className="task-row-main"
                  type="button"
                  onClick={() => goTo(task)}
                  disabled={!task.parent_id}
                >
                  <span className={`task-kind-badge task-kind-badge--${task.kind}`}>
                    {KIND_LABEL[task.kind]}
                  </span>
                  <span className="task-label">{task.label || task.id}</span>
                  <span className="task-elapsed">
                    started {formatRelativeTime(task.started_at)}
                  </span>
                  {task.has_interrupt && (
                    <span className="task-flag task-flag--interrupt">awaiting input</span>
                  )}
                  {task.cancelled && (
                    <span className="task-flag task-flag--cancelling">stopping…</span>
                  )}
                  {task.budget_exceeded && (
                    <span className="task-flag" style={{background:'var(--error-bg)', color:'var(--error-text)'}}>budget exceeded</span>
                  )}
                </button>
                <div style={{display:'flex', alignItems:'center', gap:10, padding:'0 4px 4px 12px', fontSize:'0.72rem', color:'var(--text-dim)'}}>
                  <span title={`${task.input_tokens} in / ${task.output_tokens} out`}>{task.total_tokens?.toLocaleString() ?? 0} tokens · {task.llm_calls} llm · {task.tool_calls} tools</span>
                  <div style={{flex:1, maxWidth:160, height:4, background:'var(--surface2)', borderRadius:4, overflow:'hidden'}} title={`${pct}% of 500k default budget`}>
                    <div style={{width:`${pct}%`, height:'100%', background:budgetColor, transition:'width 0.3s'}} />
                  </div>
                  {task.budget_reason && <span style={{color:'var(--error-text)', maxWidth:220, overflow:'hidden', textOverflow:'ellipsis', whiteSpace:'nowrap'}}>{task.budget_reason}</span>}
                </div>
              </div>
              <button
                className="task-stop-btn"
                type="button"
                disabled={task.cancelled || stopAction.pending}
                onClick={() => void stopAction.run(task.id)}
              >
                Stop
              </button>
            </li>
          )})}
        </ul>
      )}
    </div>
  );
}
