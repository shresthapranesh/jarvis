import {RouterProvider, createRouter} from '@tanstack/react-router';
import {StrictMode, Suspense} from 'react';
import ReactDOM from 'react-dom/client';
import {RelayEnvironmentProvider} from 'react-relay';

import {environment} from './relay/environment';
import {routeTree} from './routeTree.gen';
import {applyBodyStyles, applyTheme, resolvedTheme} from './theme/applyTheme';

import './base.css';

// StyleX's compiled CSS is appended to Vite's stylesheet asset in a build, but
// in dev there is no such asset yet — this runtime fetches /virtual:stylex.css
// and re-injects it on HMR. It also disables any stale <link> to that path, so
// index.html needs no markup of its own.
if (import.meta.env.DEV) void import('virtual:stylex:runtime');

// Before first render: the pre-paint script in index.html has already stamped
// `data-theme`, but the hashed theme class it cannot compute goes on here.
applyTheme(resolvedTheme());
applyBodyStyles();

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
