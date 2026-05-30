import {createFileRoute} from '@tanstack/react-router';
import {Suspense} from 'react';

import {ArtifactsBrowser} from '../components/ArtifactsBrowser';

export const Route = createFileRoute('/artifacts')({
  component: ArtifactsPage,
});

function ArtifactsPage() {
  return (
    <Suspense fallback={<div className="artifacts-loading">Loading artifacts…</div>}>
      <ArtifactsBrowser />
    </Suspense>
  );
}
