import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';

import {ProjectDetail} from '../components/ProjectDetail';
import {QueryBoundary} from '../components/QueryBoundary';
import {page} from '../components/ui';

export const Route = createFileRoute('/projects/$id')({component: ProjectDetailPage});

function ProjectDetailPage() {
  const {id} = Route.useParams();
  return (
    <QueryBoundary
      label="Failed to load project"
      fallback={
        <div {...stylex.props(page.scroll)}>
          <div {...stylex.props(page.empty)}>Loading…</div>
        </div>
      }
    >
      <ProjectDetail id={id} />
    </QueryBoundary>
  );
}
