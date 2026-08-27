import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';

import {QueryBoundary} from '../components/QueryBoundary';
import {TaskBoard} from '../components/TaskBoard';
import {page} from '../components/ui';

export const Route = createFileRoute('/board')({
  component: BoardPage,
});

function BoardPage() {
  return (
    <QueryBoundary
      label="Failed to load board"
      fallback={<div {...stylex.props(page.empty)}>Loading…</div>}
    >
      <TaskBoard />
    </QueryBoundary>
  );
}
