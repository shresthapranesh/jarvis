import {createFileRoute, Outlet} from '@tanstack/react-router';

import {fetchWorkflow} from '../relay/WorkflowDetailQuery';

export const Route = createFileRoute('/workflow/$id')({
  // Warms the Relay store before the page renders, so its useLazyLoadQuery
  // reads through instead of suspending.
  loader: ({params: {id}}) => fetchWorkflow(id),
  component: () => <Outlet />,
});
