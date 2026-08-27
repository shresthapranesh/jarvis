import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';
import {Suspense} from 'react';

import {ArtifactsBrowser} from '../components/ArtifactsBrowser';
import {browser} from '../components/ArtifactsBrowser.styles';

export const Route = createFileRoute('/artifacts')({
  component: ArtifactsPage,
});

function ArtifactsPage() {
  return (
    <Suspense fallback={<div {...stylex.props(browser.loading)}>Loading artifacts…</div>}>
      <ArtifactsBrowser />
    </Suspense>
  );
}
