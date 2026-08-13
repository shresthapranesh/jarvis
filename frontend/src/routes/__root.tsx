import {
  createRootRouteWithContext,
  Link,
  Outlet,
  useNavigate,
  useRouterState,
} from '@tanstack/react-router';
import {useCallback, useEffect, useRef, useState} from 'react';
import type {Environment} from 'relay-runtime';

import {ConversationList} from '../components/ConversationList';
import {QueryBoundary} from '../components/QueryBoundary';
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
  MenuIcon,
  MicIcon,
  MoonIcon,
  StarIcon,
  SunIcon,
  WorkflowIcon,
} from '../components/icons';
import {useHealth} from '../hooks/useHealth';
import {useIsMobile} from '../hooks/useIsMobile';
import {useRunningTasks} from '../hooks/useRunningTasks';
import {ToastProvider} from '../lib/toast';

interface RouterContext {
  environment: Environment;
}

const NAV_MIN_W = 180;
const NAV_MAX_W = 440;
const NAV_DEFAULT_W = 264;

/**
 * Publish the height the virtual keyboard steals as `--kb-inset` so the shell
 * can shrink and keep the composer on screen.
 *
 * Browsers honouring `interactive-widget=resizes-content` (set in index.html)
 * already shrink the layout viewport, which shrinks `innerHeight` too — so the
 * overlap computes to ~0 there and the two mechanisms never double-apply. This
 * is what covers iOS Safari, which ignores that meta directive.
 */
function useKeyboardInset() {
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const apply = () => {
      const overlap = Math.max(0, window.innerHeight - vv.height - vv.offsetTop);
      document.documentElement.style.setProperty('--kb-inset', `${overlap}px`);
    };
    apply();
    vv.addEventListener('resize', apply);
    vv.addEventListener('scroll', apply);
    return () => {
      vv.removeEventListener('resize', apply);
      vv.removeEventListener('scroll', apply);
      document.documentElement.style.removeProperty('--kb-inset');
    };
  }, []);
}

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
  const healthy = useHealth();

  const runningCount = useRunningTasks().length;

  const [navCollapsed, setNavCollapsed] = useState(
    () => localStorage.getItem('nav-collapsed') === 'true',
  );

  const isMobile = useIsMobile();
  useKeyboardInset();

  // The drawer is a separate axis from the desktop collapse: its default is
  // closed, the rail's is expanded, and persisting one must not disturb the
  // other. A user who collapsed the rail on desktop still gets a labelled
  // drawer on a phone — hence `railCollapsed` rather than `navCollapsed`.
  const [navOpen, setNavOpen] = useState(false);
  const railCollapsed = navCollapsed && !isMobile;

  const pathname = useRouterState({select: (s) => s.location.pathname});
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

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

  // Installed as a PWA the browser paints its own chrome (the Android status
  // bar, the iOS safe areas) in theme-color, so a stale value frames the app in
  // the wrong theme. Read it back off --bg rather than restating the hex here:
  // index.html carries a literal only because it runs before any stylesheet.
  const syncThemeColor = useCallback(() => {
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--bg').trim();
    if (bg) document.querySelector('meta[name="theme-color"]')?.setAttribute('content', bg);
  }, []);

  useEffect(syncThemeColor, [syncThemeColor]);

  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', next);
      syncThemeColor();
      try {
        localStorage.setItem('theme', next);
      } catch {
        /* localStorage unavailable — keep the in-memory choice */
      }
      return next;
    });
  }, [syncThemeColor]);

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
      if (e.key === 'Escape') {
        setNavOpen(false);
        return;
      }
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
        className={`left-panel${railCollapsed ? ' collapsed' : ''}${navResizing ? ' resizing' : ''}${navOpen ? ' nav-open' : ''}`}
        style={railCollapsed ? undefined : {width: navWidth}}
        aria-hidden={isMobile && !navOpen}
        inert={isMobile && !navOpen}
      >
        <div className="left-panel-header">
          <span className="brand-mark" aria-hidden="true">
            <BrandMark size={railCollapsed ? 18 : 20} />
          </span>
          {!railCollapsed && (
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
          {!railCollapsed && (
            <button
              className="icon-ghost-btn theme-toggle"
              onClick={toggleTheme}
              title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
              aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
            >
              {theme === 'dark' ? <SunIcon size={14} /> : <MoonIcon size={14} />}
            </button>
          )}
          {/* On mobile the rail cannot collapse — it is either open or off
              canvas — so the chevron closes the drawer instead. */}
          <button
            className={`icon-ghost-btn nav-toggle${railCollapsed ? ' nav-toggle--collapsed' : ''}`}
            onClick={isMobile ? () => setNavOpen(false) : toggleNav}
            title={isMobile ? 'Close menu' : railCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
            aria-label={
              isMobile ? 'Close menu' : railCollapsed ? 'Expand sidebar' : 'Collapse sidebar'
            }
          >
            <ChevronLeftIcon size={14} />
          </button>
        </div>
        <div className="left-panel-nav">
          {NAV_GROUPS.map((group) => (
            <div className="nav-group" key={group.heading}>
              {!railCollapsed && <p className="nav-heading">{group.heading}</p>}
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
                    {!railCollapsed && <span>{label}</span>}
                    {count > 0 && (
                      <span className={`nav-badge${railCollapsed ? ' nav-badge--compact' : ''}`}>
                        {count}
                      </span>
                    )}
                  </Link>
                );
              })}
            </div>
          ))}
        </div>
        {/* The sidebar renders outside every route, so an unguarded throw
            here blanks the whole app rather than one page. */}
        {!railCollapsed && (
          <QueryBoundary label="Failed to load conversations">
            <ConversationList />
          </QueryBoundary>
        )}
        {!railCollapsed && (
          <div
            className="nav-resize-handle"
            onPointerDown={startNavResize}
            onDoubleClick={resetNavWidth}
            title="Drag to resize · double-click to reset"
          />
        )}
      </aside>
      {isMobile && navOpen && (
        <button className="nav-scrim" aria-label="Close menu" onClick={() => setNavOpen(false)} />
      )}
      <main className="main-panel">
        {/* Always rendered; CSS hides it above the 860px breakpoint. */}
        <header className="mobile-topbar">
          <button
            className="icon-ghost-btn"
            onClick={() => setNavOpen(true)}
            aria-label="Open menu"
            aria-expanded={navOpen}
            title="Menu"
          >
            <MenuIcon size={18} />
          </button>
          <span className="brand-mark" aria-hidden="true">
            <BrandMark size={18} />
          </span>
          <div className="mobile-topbar-brand">
            <span className="brand">Jarvis</span>
            <span className={`brand-status${runningCount > 0 ? ' brand-status--busy' : ''}`}>
              <span className={`status-dot ${healthy ? 'ok' : 'err'}`} />
              {!healthy ? 'offline' : runningCount > 0 ? `${runningCount} running` : 'idle'}
            </span>
          </div>
          <button
            className="icon-ghost-btn theme-toggle"
            onClick={toggleTheme}
            title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
            aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
          >
            {theme === 'dark' ? <SunIcon size={16} /> : <MoonIcon size={16} />}
          </button>
        </header>
        <Outlet />
      </main>
    </div>
    </ToastProvider>
  );
}

export const Route = createRootRouteWithContext<RouterContext>()({component: RootLayout});
