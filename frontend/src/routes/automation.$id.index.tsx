import {createFileRoute} from '@tanstack/react-router';

import AutomationDetailPage from '../components/AutomationDetailPage';

export const Route = createFileRoute('/automation/$id/')({component: AutomationDetailPage});
