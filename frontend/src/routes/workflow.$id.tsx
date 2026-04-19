import {createFileRoute, Outlet} from '@tanstack/react-router';
import {getWorkflow} from '../lib/api';

export const Route = createFileRoute('/workflow/$id')({
  loader: ({context: {queryClient}, params: {id}}) =>
    queryClient.ensureQueryData({
      queryKey: ['workflow', id],
      queryFn: () => getWorkflow(id),
    }),
  component: () => <Outlet />,
});
