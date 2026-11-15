import {createFileRoute, redirect} from '@tanstack/react-router';

// The board became the Activity page; old links land there.
export const Route = createFileRoute('/board')({
  beforeLoad: () => {
    throw redirect({to: '/activity', replace: true});
  },
});
