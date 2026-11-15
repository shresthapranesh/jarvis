import {createFileRoute, redirect} from '@tanstack/react-router';

// What is running is the Activity page's Working column; old links land there.
export const Route = createFileRoute('/tasks')({
  beforeLoad: () => {
    throw redirect({to: '/activity', replace: true});
  },
});
