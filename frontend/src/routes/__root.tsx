import {useQuery, type QueryClient} from '@tanstack/react-query';
import {createRootRouteWithContext, Link, Outlet, useNavigate} from '@tanstack/react-router';
import {useCallback, useEffect, useRef, useState} from 'react';
import type {Environment} from 'relay-runtime';

import {ConversationList} from '../components/ConversationList';
import {
  BookIcon,
  BoltIcon,
  BrandMark,
  ChevronLeftIcon,
  ClockIcon,
  FileIcon,
  FolderIcon,
  GearIcon,
  KanbanIcon,
  ListIcon,
  MicIcon,
  MoonIcon,
  StarIcon,
  SunIcon,
  WorkflowIcon,
} from '../components/icons';
import {checkHealth} from '../lib/api';
import {fetchRunningTasks} from '../relay/RunningTasksQuery';
import type {RunningTask} from '../lib/types';
import {ToastProvider} from '../lib/toast';

interface RouterContext {
  queryClient: QueryClient;
  environment: Environment;
}

const NAV_MIN_W = 180;
const NAV_MAX_W = 440;
const NAV_DEFAULT_W = 264;

// Grouped so the rail reads as three intents rather than eleven equal rows:
// what the agent is *doing*, what it *knows*, and how you *tune* it.
interface NavItem {
  to: string;
  label: string;
  Icon: (p: {size?: number}) => React.ReactElement;
  /** Only Tasks carries a live count today; keyed so more can opt in. */
  badge?: 'running';
}

const NAV_GROUPS: {heading: string; items: NavItem[]}[] = [
  {
    heading: 'Work',
    items: [
      {to: '/live', label: 'Live', Icon: MicIcon},
      {to: '/board', label: 'Board', Icon: KanbanIcon},
      {to: '/workflow', label: 'Workflows', Icon: WorkflowIcon},
      {to: '/automation', label: 'Automations', Icon: BoltIcon},
      {to: '/tasks', label: 'Tasks', Icon: ClockIcon, badge: 'running'},
    ],
  },
  {
    heading: 'Context',
    items: [
      {to: '/projects', label: 'Projects', Icon: FolderIcon},
      {to: '/artifacts', label: 'Artifacts', Icon: FileIcon},
      {to: '/memory', label: 'Memory', Icon: BookIcon},
      {to: '/skills', label: 'Skills', Icon: StarIcon},
    ],
  },
  {
    heading: 'System',
    items: [
      {to: '/logs', label: 'Logs', Icon: ListIcon},
      {to: '/settings', label: 'Settings', Icon: GearIcon},
    ],
  },
];

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

  const [navWidth, setNavWidth] = useState(() => {
    const saved = Number(localStorage.getItem('nav-width'));
    return Number.isFinite(saved) ? Math.min(NAV_MAX_W, Math.max(NAV_MIN_W, saved)) : NAV_DEFAULT_W;
  });
  const [navResizing, setNavResizing] = useState(false);
  const navWidthRef = useRef(navWidth);
  navWidthRef.current = navWidth;

  const startNavResize = useCallback(() => {
    setNavResizing(true);
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    const onMove = (ev: PointerEvent) => {
      const w = Math.min(NAV_MAX_W, Math.max(NAV_MIN_W, ev.clientX));
      navWidthRef.current = w;
      setNavWidth(w);
    };
    const onUp = () => {
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', onUp);
      setNavResizing(false);
      document.body.style.cursor = '';
      document.body.style.userSelect = '';
      localStorage.setItem('nav-width', String(navWidthRef.current));
    };
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', onUp);
  }, []);

  const resetNavWidth = useCallback(() => {
    setNavWidth(NAV_DEFAULT_W);
    localStorage.setItem('nav-width', String(NAV_DEFAULT_W));
  }, []);

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

  // Wrap TanStack Router navigations in View Transitions when available
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const a = (e.target as HTMLElement)?.closest('a[href]');
      if (!a) return;
      if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
      const url = new URL(a.getAttribute('href')!, location.href);
      if (url.origin !== location.origin) return;
      if (!('startViewTransition' in document)) return;
      if (window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
      e.preventDefault();
      document.startViewTransition(() => {
        navigate({to: url.pathname + url.search as any});
      });
    };
    document.addEventListener('click', handler);
    return () => document.removeEventListener('click', handler);
  }, [navigate]);

  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      const mod = e.metaKey || e.ctrlKey;
      if (!mod) return;

      if (e.key === 'k' || e.key === 'K') {
        e.preventDefault();
        const doNav = () => navigate({to: '/'});
        if (
          'startViewTransition' in document &&
          !window.matchMedia('(prefers-reduced-motion: reduce)').matches
        )
          document.startViewTransition(doNav);
        else doNav();
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
      <aside
        className={`left-panel${navCollapsed ? ' collapsed' : ''}${navResizing ? ' resizing' : ''}`}
        style={navCollapsed ? undefined : {width: navWidth}}
      >
        <div className="left-panel-header">
          <span className="brand-mark" aria-hidden="true">
            <BrandMark size={navCollapsed ? 18 : 20} />
          </span>
          {!navCollapsed && (
            <div className="brand-block">
              <span className="brand">Jarvis</span>
              <span className={`brand-status${runningCount > 0 ? ' brand-status--busy' : ''}`}>
                <span className={`status-dot ${healthy ? 'ok' : 'err'}`} />
                {!healthy
                  ? 'offline'
                  : runningCount > 0
                    ? `${runningCount} running`
                    : 'idle'}
              </span>
            </div>
          )}
          {!navCollapsed && (
            <button
              className="icon-ghost-btn theme-toggle"
              onClick={toggleTheme}
              title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            >
              {theme === 'dark' ? <SunIcon size={14} /> : <MoonIcon size={14} />}
            </button>
          )}
          <button
            className={`icon-ghost-btn nav-toggle${navCollapsed ? ' nav-toggle--collapsed' : ''}`}
            onClick={toggleNav}
            title={navCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-label={navCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          >
            <ChevronLeftIcon size={14} />
          </button>
        </div>
        <div className="left-panel-nav">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.heading}>
              {!navCollapsed && <p className="nav-heading">{group.heading}</p>}
              {group.items.map(({to, label, Icon, badge}) => {
                const count = badge === 'running' ? runningCount : 0;
                return (
                  <Link
                    key={to}
                    to={to}
                    className="nav-link"
                    activeProps={{className: 'nav-link active'}}
                    title={label}
                  >
                    <Icon size={15} />
                    {!navCollapsed && <span>{label}</span>}
                    {count > 0 && (
                      <span className={`nav-badge${navCollapsed ? ' nav-badge--compact' : ''}`}>
                        {count}
                      </span>
                    )}
                  </Link>
                );
              })}
            </div>
          ))}
        </div>
        {!navCollapsed && <ConversationList />}
        {!navCollapsed && (
          <div
            className="nav-resize-handle"
            onPointerDown={startNavResize}
            onDoubleClick={resetNavWidth}
            title="Drag to resize · double-click to reset"
          />
        )}
      </aside>
      <main className="main-panel">
        <Outlet />
      </main>
    </div>
    </ToastProvider>
  );
}

export const Route = createRootRouteWithContext<RouterContext>()({component: RootLayout});
