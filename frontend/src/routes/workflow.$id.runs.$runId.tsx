import {createFileRoute} from '@tanstack/react-router';
import {lazy, Suspense} from 'react';
import {fetchWorkflowRun} from '../relay/WorkflowRunDetailQuery';

const WorkflowRunPage = lazy(() => import('../components/WorkflowRunPage'));

export const Route = createFileRoute('/workflow/$id/runs/$runId')({
  loader: ({context: {queryClient}, params: {runId}}) =>
    queryClient.ensureQueryData({
      queryKey: ['workflow-run', runId],
      queryFn: () => fetchWorkflowRun(runId),
    }),
  component: () => (
    <Suspense fallback={<div style={{padding: 24, color: 'var(--text-dim)'}}>Loading run…</div>}>
      <WorkflowRunPage />
    </Suspense>
  ),
});
