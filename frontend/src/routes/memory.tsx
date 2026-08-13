import {createFileRoute} from '@tanstack/react-router';

import {MemoryView} from '../components/MemoryView';
import {QueryBoundary} from '../components/QueryBoundary';

export const Route = createFileRoute('/memory')({
  component: MemoryPage,
});

function MemoryPage() {
  return (
    <QueryBoundary
      label="Failed to load memory"
      fallback={<div className="memory-empty">Loading…</div>}
    >
      <MemoryView />
    </QueryBoundary>
  );
}
