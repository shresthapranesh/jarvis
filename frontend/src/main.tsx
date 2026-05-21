import {QueryClient, QueryClientProvider} from '@tanstack/react-query';
import {RouterProvider, createRouter} from '@tanstack/react-router';
import {StrictMode, Suspense} from 'react';
import ReactDOM from 'react-dom/client';
import {RelayEnvironmentProvider} from 'react-relay';

import {environment} from './relay/environment';
import {routeTree} from './routeTree.gen';

import './styles.css';

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {staleTime: 30_000, retry: 1},
  },
});

const router = createRouter({
  routeTree,
  context: {queryClient, environment},
});

declare module '@tanstack/react-router' {
  interface Register {
    router: typeof router;
  }
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RelayEnvironmentProvider environment={environment}>
      <QueryClientProvider client={queryClient}>
        <Suspense fallback={null}>
          <RouterProvider router={router} context={{queryClient, environment}} />
        </Suspense>
      </QueryClientProvider>
    </RelayEnvironmentProvider>
  </StrictMode>,
);
