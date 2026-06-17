import {useQuery, type QueryClient} from '@tanstack/react-query';
import {createRootRouteWithContext, Link, Outlet, useNavigate} from '@tanstack/react-router';
import {useCallback, useEffect, useState} from 'react';
import type {Environment} from 'relay-runtime';

import {ConversationList} from '../components/ConversationList';
import {checkHealth} from '../lib/api';
import {fetchRunningTasks} from '../relay/RunningTasksQuery';
import type {RunningTask} from '../lib/types';
import {ToastProvider} from '../lib/toast';

interface RouterContext {
  queryClient: QueryClient;
  environment: Environment;
}

function RootLayout() {
  const {data} = useQuery({
    queryKey: ['health'],
    queryFn: checkHealth,
    staleTime: Infinity,
    retry: false
  });

  const healthy = data?.status === 'ok';

  const {data: runningTasks} = useQuery({
    queryKey: ['running-tasks'],
    queryFn: fetchRunningTasks,
    refetchInterval: (query) => ((query.state.data as RunningTask[] | undefined)?.length ?? 0) > 0 ? 2000 : false,
  });
  const runningCount = runningTasks?.length ?? 0;

  const [navCollapsed, setNavCollapsed] = useState(
    () => localStorage.getItem('nav-collapsed') === 'true',
  );

  const navigate = useNavigate();

  const toggleNav = useCallback(() => {
    const next = !navCollapsed;
    setNavCollapsed(next);
    localStorage.setItem('nav-collapsed', String(next));
  }, [navCollapsed]);

  // Theme is resolved before first paint by the inline script in index.html;
  // seed state from the attribute it set, then persist the user's choice.
  const [theme, setTheme] = useState<'light' | 'dark'>(
    () => (document.documentElement.getAttribute('data-theme') as 'light' | 'dark') ?? 'dark',
  );

  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      try {
        localStorage.setItem('theme', next);
      } catch {
        /* localStorage unavailable — keep the in-memory choice */
      }
      return next;
    });
  }, []);

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
    <ToastProvider>
    <div className="app-shell">
      <aside className={`left-panel${navCollapsed ? ' collapsed' : ''}`}>
        <div className="left-panel-header">
          <div className={`status-dot ${healthy ? 'ok' : 'err'}`} />
          {!navCollapsed && <span className="brand">Assistant</span>}
          {!navCollapsed && (
            <button
              className="theme-toggle"
              onClick={toggleTheme}
              title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            >
              {theme === 'dark' ? (
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <circle cx="12" cy="12" r="4" />
                  <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
                </svg>
              ) : (
                <svg
                  width="13"
                  height="13"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
                </svg>
              )}
            </button>
          )}
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
          <Link
            to="/artifacts"
            className="nav-link"
            activeProps={{className: 'nav-link active'}}
            title="Artifacts"
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
              <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
              <polyline points="14 2 14 8 20 8" />
            </svg>
            {!navCollapsed && <span>Artifacts</span>}
          </Link>
          <Link
            to="/tasks"
            className="nav-link"
            activeProps={{className: 'nav-link active'}}
            title="Tasks"
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
              <circle cx="12" cy="12" r="9" />
              <polyline points="12 7 12 12 15 14" />
            </svg>
            {!navCollapsed && <span>Tasks</span>}
            {runningCount > 0 && (
              <span className={`nav-badge${navCollapsed ? ' nav-badge--compact' : ''}`}>
                {runningCount}
              </span>
            )}
          </Link>
          <Link
            to="/memory"
            className="nav-link"
            activeProps={{className: 'nav-link active'}}
            title="Memory"
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
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" />
              <path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
            </svg>
            {!navCollapsed && <span>Memory</span>}
          </Link>
          <Link
            to="/logs"
            className="nav-link"
            activeProps={{className: 'nav-link active'}}
            title="Logs"
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
              <path d="M4 6h16" />
              <path d="M4 12h16" />
              <path d="M4 18h10" />
            </svg>
            {!navCollapsed && <span>Logs</span>}
          </Link>
          <Link
            to="/settings"
            className="nav-link"
            activeProps={{className: 'nav-link active'}}
            title="Settings"
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
              <circle cx="12" cy="12" r="3" />
              <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z" />
            </svg>
            {!navCollapsed && <span>Settings</span>}
          </Link>
        </div>
        {!navCollapsed && <ConversationList />}
      </aside>
      <main className="main-panel">
        <Outlet />
      </main>
    </div>
    </ToastProvider>
  );
}

export const Route = createRootRouteWithContext<RouterContext>()({component: RootLayout});
