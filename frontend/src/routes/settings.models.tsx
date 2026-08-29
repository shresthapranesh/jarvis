import {createFileRoute} from '@tanstack/react-router';

import {ModelsTab} from '../components/settings/ModelsTab';

export const Route = createFileRoute('/settings/models')({component: ModelsTab});
