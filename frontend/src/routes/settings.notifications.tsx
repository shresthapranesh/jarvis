import {createFileRoute} from '@tanstack/react-router';

import {NotificationsTab} from '../components/settings/NotificationsTab';

export const Route = createFileRoute('/settings/notifications')({component: NotificationsTab});
