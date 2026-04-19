import {useQuery, type QueryClient} from '@tanstack/react-query';
import {createRootRouteWithContext, Link, Outlet, useNavigate} from '@tanstack/react-router';
import {useCallback, useEffect, useState} from 'react';

import {ConversationList} from '../components/ConversationList';
import {checkHealth} from '../lib/api';

interface RouterContext {
  queryClient: QueryClient;
}

function RootLayout() {
  const {data} = useQuery({
    queryKey: ['health'],
    queryFn: checkHealth,
    staleTime: Infinity,
    retry: false
  });

  const healthy = data?.status === 'ok';

  const [navCollapsed, setNavCollapsed] = useState(
    () => localStorage.getItem('nav-collapsed') === 'true',
  );

  const navigate = useNavigate();

  const toggleNav = useCallback(() => {
    const next = !navCollapsed;
    setNavCollapsed(next);
    localStorage.setItem('nav-collapsed', String(next));
  }, [navCollapsed]);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;

      if (e.key === 'k' || e.key === 'K') {
        e.preventDefault();
        navigate({to: '/'});
        setTimeout(() => {
          (document.querySelector('.input-textarea') as HTMLElement | null)?.focus();
        }, 50);
      } else if (e.key === '/') {
        e.preventDefault();
        (document.querySelector('.input-textarea') as HTMLElement | null)?.focus();
      } else if (e.key === '[') {
        e.preventDefault();
        toggleNav();
      }
    }

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [navigate, toggleNav]);

  return (
    <div className="app-shell">
      <aside className={`left-panel${navCollapsed ? ' collapsed' : ''}`}>
        <div className="left-panel-header">
          <div className={`status-dot ${healthy ? 'ok' : 'err'}`} />
          {!navCollapsed && <span className="brand">Assistant</span>}
          <button
            className={`nav-toggle${navCollapsed ? ' nav-toggle--collapsed' : ''}`}
            onClick={toggleNav}
            title={navCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.5"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="15 18 9 12 15 6" />
            </svg>
          </button>
        </div>
        <div className="left-panel-nav">
          <Link
            to="/automation"
            className="nav-link"
            activeProps={{className: 'nav-link active'}}
            title="Automations"
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
            </svg>
            {!navCollapsed && <span>Automations</span>}
          </Link>
          <Link
            to="/live"
            className="nav-link"
            activeProps={{className: 'nav-link active'}}
            title="Live"
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
              <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
              <line x1="12" y1="19" x2="12" y2="23" />
              <line x1="8" y1="23" x2="16" y2="23" />
            </svg>
            {!navCollapsed && <span>Live</span>}
          </Link>
          <Link
            to="/workflow"
            className="nav-link"
            activeProps={{className: 'nav-link active'}}
            title="Workflows"
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2.2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <rect x="3" y="3" width="6" height="6" rx="1" />
              <rect x="15" y="3" width="6" height="6" rx="1" />
              <rect x="9" y="15" width="6" height="6" rx="1" />
              <line x1="6" y1="9" x2="12" y2="15" />
              <line x1="18" y1="9" x2="12" y2="15" />
            </svg>
            {!navCollapsed && <span>Workflows</span>}
          </Link>
        </div>
        {!navCollapsed && <ConversationList />}
      </aside>
      <main className="main-panel">
        <Outlet />
      </main>
    </div>
  );
}

export const Route = createRootRouteWithContext<RouterContext>()({component: RootLayout});
