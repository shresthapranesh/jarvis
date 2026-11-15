import type {LinkProps} from '@tanstack/react-router';

import type {RunningTask} from './types';

/**
 * Where a running task is watched live: the conversation it answers in, or
 * the run page of its automation or workflow. `null` when the task names
 * nothing to open.
 */
export function runLink(task: RunningTask): LinkProps | null {
  const parent = task.parent_id;
  if (!parent) return task.kind === 'board_task' ? {to: '/board'} : null;
  // A board task's run answers in the task's own conversation.
  if (task.kind === 'board_task') return {to: '/c/$id', params: {id: `boardtask_${parent}`}};
  if (task.kind === 'chat') return {to: '/c/$id', params: {id: parent}};
  if (task.kind === 'automation') {
    return {to: '/automation/$id/runs/$runId', params: {id: parent, runId: task.id}};
  }
  if (task.kind === 'workflow') {
    return {to: '/workflow/$id/runs/$runId', params: {id: parent, runId: task.id}};
  }
  return null;
}
