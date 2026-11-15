import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';

import {QueryBoundary} from '../components/QueryBoundary';
import {TaskBoard} from '../components/TaskBoard';
import {page} from '../components/ui';

export const Route = createFileRoute('/activity')({
  component: ActivityPage,
});

function ActivityPage() {
  return (
    <QueryBoundary
      label="Failed to load activity"
      fallback={<div {...stylex.props(page.empty)}>Loading…</div>}
    >
      <TaskBoard />
    </QueryBoundary>
  );
}
