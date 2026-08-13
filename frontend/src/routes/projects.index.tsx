import {createFileRoute} from '@tanstack/react-router';

import {ProjectsView} from '../components/ProjectsView';
import {QueryBoundary} from '../components/QueryBoundary';

export const Route = createFileRoute('/projects/')({component: ProjectsPage});

function ProjectsPage() {
  return (
    <QueryBoundary
      label="Failed to load projects"
      fallback={<div className="memory-empty">Loading…</div>}
    >
      <ProjectsView />
    </QueryBoundary>
  );
}
