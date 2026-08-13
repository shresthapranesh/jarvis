import {createFileRoute} from '@tanstack/react-router';

import {QueryBoundary} from '../components/QueryBoundary';
import {SkillsView} from '../components/SkillsView';

export const Route = createFileRoute('/skills')({
  component: SkillsPage,
});

function SkillsPage() {
  return (
    <QueryBoundary
      label="Failed to load skills"
      fallback={<div className="memory-empty">Loading…</div>}
    >
      <SkillsView />
    </QueryBoundary>
  );
}
