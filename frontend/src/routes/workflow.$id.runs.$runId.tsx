import {createFileRoute} from '@tanstack/react-router';
import {lazy} from 'react';

import {QueryBoundary} from '../components/QueryBoundary';
import {fetchWorkflowRun} from '../relay/WorkflowRunDetailQuery';

const WorkflowRunPage = lazy(() => import('../components/WorkflowRunPage'));

export const Route = createFileRoute('/workflow/$id/runs/$runId')({
  loader: ({params: {runId}}) => fetchWorkflowRun(runId),
  component: () => (
    <QueryBoundary
      label="Failed to load run"
      fallback={<div style={{padding: 24, color: 'var(--text-dim)'}}>Loading run…</div>}
    >
      <WorkflowRunPage />
    </QueryBoundary>
  ),
});
