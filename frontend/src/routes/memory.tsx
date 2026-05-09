import {createFileRoute} from '@tanstack/react-router';

import {MemoryView} from '../components/MemoryView';

export const Route = createFileRoute('/memory')({
  component: MemoryView,
});
