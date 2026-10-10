import {createFileRoute} from '@tanstack/react-router';

import AutomationRunPage from '../components/AutomationRunPage';

export const Route = createFileRoute('/automation/$id/runs/$runId')({component: AutomationRunPage});
