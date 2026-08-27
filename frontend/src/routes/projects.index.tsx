import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';

import {ProjectsView} from '../components/ProjectsView';
import {QueryBoundary} from '../components/QueryBoundary';
import {page} from '../components/ui';

export const Route = createFileRoute('/projects/')({component: ProjectsPage});

function ProjectsPage() {
  return (
    <QueryBoundary
      label="Failed to load projects"
      fallback={<div {...stylex.props(page.empty)}>Loading…</div>}
    >
      <ProjectsView />
    </QueryBoundary>
  );
}
