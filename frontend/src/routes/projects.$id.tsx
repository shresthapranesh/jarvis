import {createFileRoute} from '@tanstack/react-router';

import {ProjectDetail} from '../components/ProjectDetail';
import {QueryBoundary} from '../components/QueryBoundary';

export const Route = createFileRoute('/projects/$id')({component: ProjectDetailPage});

function ProjectDetailPage() {
  const {id} = Route.useParams();
  return (
    <QueryBoundary
      label="Failed to load project"
      fallback={<div className="page memory-page"><div className="memory-empty">Loading…</div></div>}
    >
      <ProjectDetail id={id} />
    </QueryBoundary>
  );
}
