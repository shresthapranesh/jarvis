import {createFileRoute} from '@tanstack/react-router';

import {ConfigTab} from '../components/settings/ConfigTab';

export const Route = createFileRoute('/settings/config')({component: ConfigTab});
