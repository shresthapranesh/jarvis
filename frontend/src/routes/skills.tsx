import {createFileRoute} from '@tanstack/react-router';

import {SkillsView} from '../components/SkillsView';

export const Route = createFileRoute('/skills')({
  component: SkillsView,
});
