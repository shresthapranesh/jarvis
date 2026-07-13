import {createFileRoute} from '@tanstack/react-router';

import {ProjectDetail} from '../components/ProjectDetail';

export const Route = createFileRoute('/projects/$id')({component: ProjectDetailPage});

function ProjectDetailPage() {
  const {id} = Route.useParams();
  return <ProjectDetail id={id} />;
}
