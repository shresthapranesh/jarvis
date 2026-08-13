import {RouterProvider, createRouter} from '@tanstack/react-router';
import {StrictMode, Suspense} from 'react';
import ReactDOM from 'react-dom/client';
import {RelayEnvironmentProvider} from 'react-relay';

import {environment} from './relay/environment';
import {routeTree} from './routeTree.gen';

import './styles.css';

const router = createRouter({
  routeTree,
  context: {environment},
});

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RelayEnvironmentProvider environment={environment}>
      <Suspense fallback={null}>
        <RouterProvider router={router} context={{environment}} />
      </Suspense>
    </RelayEnvironmentProvider>
  </StrictMode>,
);
