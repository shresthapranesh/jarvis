import {createFileRoute} from '@tanstack/react-router';

import {QueryBoundary} from '../components/QueryBoundary';
import {TaskBoard} from '../components/TaskBoard';

export const Route = createFileRoute('/board')({
  component: BoardPage,
});

function BoardPage() {
  return (
    <QueryBoundary label="Failed to load board" fallback={<div className="memory-empty">Loading…</div>}>
      <TaskBoard />
    </QueryBoundary>
  );
}
