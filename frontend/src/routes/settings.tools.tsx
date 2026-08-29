import {createFileRoute} from '@tanstack/react-router';

import {ToolsTab} from '../components/settings/ToolsTab';

export const Route = createFileRoute('/settings/tools')({component: ToolsTab});
