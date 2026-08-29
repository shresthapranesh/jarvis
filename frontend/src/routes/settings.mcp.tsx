import {createFileRoute} from '@tanstack/react-router';

import {McpTab} from '../components/settings/McpTab';

export const Route = createFileRoute('/settings/mcp')({component: McpTab});
