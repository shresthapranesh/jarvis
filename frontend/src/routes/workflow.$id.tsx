import {createFileRoute, Outlet} from '@tanstack/react-router';
import {fetchWorkflow} from '../relay/WorkflowDetailQuery';

export const Route = createFileRoute('/workflow/$id')({
  loader: ({context: {queryClient}, params: {id}}) =>
    queryClient.ensureQueryData({
      queryKey: ['workflow', id],
      queryFn: () => fetchWorkflow(id),
    }),
  component: () => <Outlet />,
});
