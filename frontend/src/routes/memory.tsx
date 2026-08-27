import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';

import {MemoryView} from '../components/MemoryView';
import {QueryBoundary} from '../components/QueryBoundary';
import {page} from '../components/ui';

export const Route = createFileRoute('/memory')({
  component: MemoryPage,
});

function MemoryPage() {
  return (
    <QueryBoundary
      label="Failed to load memory"
      fallback={<div {...stylex.props(page.empty)}>Loading…</div>}
    >
      <MemoryView />
    </QueryBoundary>
  );
}
