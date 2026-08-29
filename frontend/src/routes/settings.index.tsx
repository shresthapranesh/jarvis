import {createFileRoute, redirect} from '@tanstack/react-router';

// `/settings` is a layout with no content of its own — land on the first tab.
export const Route = createFileRoute('/settings/')({
  beforeLoad: () => {
    throw redirect({to: '/settings/mcp', replace: true});
  },
});
