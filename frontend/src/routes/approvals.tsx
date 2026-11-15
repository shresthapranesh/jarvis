import {createFileRoute, redirect} from '@tanstack/react-router';

// Requests waiting on you are the Activity page's Needs me column; old links land there.
export const Route = createFileRoute('/approvals')({
  beforeLoad: () => {
    throw redirect({to: '/activity', replace: true});
  },
});
