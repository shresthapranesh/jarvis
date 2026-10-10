import {createFileRoute, Outlet} from '@tanstack/react-router';

export const Route = createFileRoute('/automation')({component: AutomationLayout});

function AutomationLayout() {
  return <Outlet />;
}
