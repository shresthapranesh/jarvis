import {createFileRoute} from '@tanstack/react-router';

import {MaintenanceTab} from '../components/settings/MaintenanceTab';

export const Route = createFileRoute('/settings/maintenance')({component: MaintenanceTab});
