import {createFileRoute} from '@tanstack/react-router';

import {TaskBoard} from '../components/TaskBoard';

export const Route = createFileRoute('/board')({
  component: TaskBoard,
});
