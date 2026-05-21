import {createFileRoute} from '@tanstack/react-router';
import {lazy, Suspense} from 'react';

const WorkflowEditorPage = lazy(() => import('../components/WorkflowEditorPage'));

export const Route = createFileRoute('/workflow/$id/')({
  component: () => (
    <Suspense fallback={<div style={{padding: 24, color: 'var(--text-dim)'}}>Loading editor…</div>}>
      <WorkflowEditorPage />
    </Suspense>
  ),
});
