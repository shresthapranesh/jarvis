import {createFileRoute} from '@tanstack/react-router';

import {ProjectsView} from '../components/ProjectsView';

export const Route = createFileRoute('/projects/')({component: ProjectsView});
