import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {createFileRoute, useNavigate} from '@tanstack/react-router';

import {formatRelativeTime} from '../lib/api';
import {fetchRunningTasks} from '../relay/RunningTasksQuery';
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
  const queryClient = useQueryClient();
  const navigate = useNavigate();

  const {data: tasks = [], isLoading} = useQuery({
    queryKey: ['running-tasks'],
    queryFn: fetchRunningTasks,
    refetchInterval: (query) => ((query.state.data as RunningTask[] | undefined)?.length ?? 0) > 0 ? 2000 : false,
  });

  const stopMutation = useMutation({
    mutationFn: (id: string) => commitStopRunningTask(id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({queryKey: ['running-tasks']});
    },
  });

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

      {isLoading ? (
        <div className="tasks-empty">Loading…</div>
      ) : tasks.length === 0 ? (
        <div className="tasks-empty">No active tasks.</div>
      ) : (
        <ul className="tasks-list">
          {tasks.map((task) => (
            <li key={task.id} className="task-row">
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
              </button>
              <button
                className="task-stop-btn"
                type="button"
                disabled={task.cancelled || stopMutation.isPending}
                onClick={() => stopMutation.mutate(task.id)}
              >
                Stop
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
