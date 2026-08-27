import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';

import {QueryBoundary} from '../components/QueryBoundary';
import {SkillsView} from '../components/SkillsView';
import {page} from '../components/ui';

export const Route = createFileRoute('/skills')({
  component: SkillsPage,
});

function SkillsPage() {
  return (
    <QueryBoundary
      label="Failed to load skills"
      fallback={<div {...stylex.props(page.empty)}>Loading…</div>}
    >
      <SkillsView />
    </QueryBoundary>
  );
}
