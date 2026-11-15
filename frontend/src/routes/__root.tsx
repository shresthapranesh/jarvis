import * as stylex from '@stylexjs/stylex';
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
import {
  BookIcon,
  BoltIcon,
  BrandMark,
  ChevronDownIcon,
  ChevronLeftIcon,
  ClockIcon,
  FileIcon,
  FolderIcon,
  GearIcon,
  KanbanIcon,
  ListIcon,
  MenuIcon,
  MoonIcon,
  PlusIcon,
  ShieldCheckIcon,
  StarIcon,
  SunIcon,
  WorkflowIcon,
} from '../components/icons';
import {QueryBoundary} from '../components/QueryBoundary';
import {useHealth} from '../hooks/useHealth';
import {useIsMobile} from '../hooks/useIsMobile';
import {usePendingApprovals} from '../hooks/usePendingApprovals';
import {useRunningTasks} from '../hooks/useRunningTasks';
import {useCurrentPageTitle} from '../lib/pageTitle';
import {ToastProvider} from '../lib/toast';
import {applyTheme, resolvedTheme, type Theme} from '../theme/applyTheme';
import {brand, control, mobile, nav, rail, shell} from './__root.styles';

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
  Icon: (p: {size?: number; style?: stylex.StyleXStyles}) => React.ReactElement;
  /** Which live count to hang off this row, if any. */
  badge?: 'running' | 'approvals';
}

interface NavGroup {
  heading: string;
  items: NavItem[];
  /** Sits below the conversations and folds to its heading. */
  secondary?: boolean;
}

const NAV_GROUPS: NavGroup[] = [
  {
    heading: 'Work',
    items: [
      {to: '/board', label: 'Board', Icon: KanbanIcon},
      {to: '/workflow', label: 'Workflows', Icon: WorkflowIcon},
      {to: '/automation', label: 'Automations', Icon: BoltIcon},
      {to: '/tasks', label: 'Running', Icon: ClockIcon, badge: 'running'},
      {to: '/approvals', label: 'Approvals', Icon: ShieldCheckIcon, badge: 'approvals'},
    ],
  },
  {
    heading: 'Context',
    secondary: true,
    items: [
      {to: '/projects', label: 'Projects', Icon: FolderIcon},
      {to: '/artifacts', label: 'Artifacts', Icon: FileIcon},
      {to: '/memory', label: 'Memory', Icon: BookIcon},
      {to: '/skills', label: 'Skills', Icon: StarIcon},
    ],
  },
  {
    heading: 'System',
    secondary: true,
    items: [
      {to: '/logs', label: 'Logs', Icon: ListIcon},
      {to: '/settings', label: 'Settings', Icon: GearIcon},
    ],
  },
];

// The rail is ordered by how often each part is reached for: starting a chat,
// the work in flight, the conversations themselves, and only then the
// reference and system pages.
const PRIMARY_GROUPS = NAV_GROUPS.filter((g) => !g.secondary);
const SECONDARY_GROUPS = NAV_GROUPS.filter((g) => g.secondary);

const IS_MAC = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

function readFoldedGroups(): string[] {
  try {
    const raw = localStorage.getItem('nav-groups-folded');
    if (raw !== null) {
      const saved: unknown = JSON.parse(raw);
      return Array.isArray(saved) ? saved.filter((h) => typeof h === 'string') : [];
    }
  } catch {
    /* fall through to the default */
  }
  // Until the user chooses: open on a desktop, where there is room for both,
  // folded in the phone drawer, where open groups leave the conversations
  // about three rows.
  return window.matchMedia('(max-width: 860px)').matches
    ? SECONDARY_GROUPS.map((g) => g.heading)
    : [];
}

function focusComposer() {
  setTimeout(() => {
    document.querySelector<HTMLElement>('[data-input-textarea]')?.focus();
  }, 50);
}

function RootLayout() {
  const healthy = useHealth();

  const runningCount = useRunningTasks().length;
  const approvalCount = usePendingApprovals().length;

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

  // The tab's title: what this page is, and — first, where a narrow tab still
  // shows it — how many things are waiting on the user. The count is the
  // Approvals badge's, so the two never disagree.
  const pageName = useCurrentPageTitle();
  useEffect(() => {
    const section = NAV_GROUPS.flatMap((g) => g.items).find(
      (i) => pathname === i.to || pathname.startsWith(`${i.to}/`),
    )?.label;
    const name = pageName ?? section;
    const base = name ? `${name} · Jarvis` : 'Jarvis';
    document.title = approvalCount > 0 ? `(${approvalCount}) ${base}` : base;
  }, [pageName, pathname, approvalCount]);
  useEffect(() => {
    setNavOpen(false);
  }, [pathname]);

  const [navWidth, setNavWidth] = useState(() => {
    // `Number(null)` is 0, which the clamp would turn into the minimum width,
    // so nothing saved has to mean the default rather than a saved 0.
    const saved = Number(localStorage.getItem('nav-width') ?? NaN);
    return Number.isFinite(saved) && saved > 0
      ? Math.min(NAV_MAX_W, Math.max(NAV_MIN_W, saved))
      : NAV_DEFAULT_W;
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

  const [foldedGroups, setFoldedGroups] = useState(readFoldedGroups);
  const toggleGroup = useCallback((heading: string) => {
    setFoldedGroups((prev) => {
      const next = prev.includes(heading) ? prev.filter((h) => h !== heading) : [...prev, heading];
      try {
        localStorage.setItem('nav-groups-folded', JSON.stringify(next));
      } catch {
        /* localStorage unavailable — keep the in-memory choice */
      }
      return next;
    });
  }, []);

  const navigate = useNavigate();

  const toggleNav = useCallback(() => {
    const next = !navCollapsed;
    setNavCollapsed(next);
    localStorage.setItem('nav-collapsed', String(next));
  }, [navCollapsed]);

  // Theme is resolved before first paint by the inline script in index.html;
  // seed state from the attribute it set, then persist the user's choice.
  const [theme, setTheme] = useState<Theme>(resolvedTheme);

  // Installed as a PWA the browser paints its own chrome (the Android status
  // bar, the iOS safe areas) in theme-color, so a stale value frames the app in
  // the wrong theme. Read it back off --pp-bg rather than restating the hex
  // here: base.css carries that literal because it has to apply before the
  // bundle (and so the hashed StyleX theme class) exists.
  const syncThemeColor = useCallback(() => {
    const bg = getComputedStyle(document.documentElement).getPropertyValue('--pp-bg').trim();
    if (bg) document.querySelector('meta[name="theme-color"]')?.setAttribute('content', bg);
  }, []);

  useEffect(syncThemeColor, [syncThemeColor]);

  const toggleTheme = useCallback(() => {
    setTheme((prev) => {
      const next = prev === 'dark' ? 'light' : 'dark';
      applyTheme(next);
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
        navigate({to: (url.pathname + url.search) as any});
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
        focusComposer();
      } else if (e.key === '/') {
        e.preventDefault();
        document.querySelector<HTMLElement>('[data-input-textarea]')?.focus();
      } else if (e.key === '[') {
        e.preventDefault();
        toggleNav();
      }
    }

    document.addEventListener('keydown', onKeyDown);
    return () => document.removeEventListener('keydown', onKeyDown);
  }, [navigate, toggleNav]);

  const status = (
    <span {...stylex.props(brand.status, runningCount > 0 && brand.statusBusy)}>
      <span
        {...stylex.props(
          brand.dot,
          healthy === true && brand.dotOk,
          healthy === false && brand.dotErr,
          healthy === true && runningCount > 0 && brand.dotPulsing,
        )}
        title={healthy === null ? 'Checking the server…' : undefined}
      />
      {healthy === null
        ? null
        : !healthy
          ? 'offline'
          : runningCount > 0
            ? `${runningCount} running`
            : 'idle'}
    </span>
  );

  const themeButton = (size: number) => (
    <button
      {...stylex.props(control.ghost, control.themeToggle)}
      onClick={toggleTheme}
      title={theme === 'dark' ? 'Switch to light' : 'Switch to dark'}
      aria-label={theme === 'dark' ? 'Switch to light theme' : 'Switch to dark theme'}
    >
      {theme === 'dark' ? (
        <SunIcon size={size} style={control.themeIcon} />
      ) : (
        <MoonIcon size={size} style={control.themeIcon} />
      )}
    </button>
  );

  const navGroups = (groups: NavGroup[], secondary = false) => (
    <div
      {...stylex.props(
        nav.list,
        secondary && nav.listSecondary,
        railCollapsed && nav.listCollapsed,
      )}
    >
      {groups.map((group) => {
        // The icon rail has no headings to fold under, so it shows every row.
        const folded = !railCollapsed && !!group.secondary && foldedGroups.includes(group.heading);
        return (
          <div {...stylex.props(nav.group)} key={group.heading}>
            {!railCollapsed &&
              (group.secondary ? (
                <button
                  {...stylex.props(nav.heading, nav.headingButton)}
                  onClick={() => toggleGroup(group.heading)}
                  aria-expanded={!folded}
                >
                  {group.heading}
                  <ChevronDownIcon
                    size={12}
                    style={[nav.headingChevron, folded && nav.headingChevronFolded]}
                  />
                </button>
              ) : (
                <p {...stylex.props(nav.heading)}>{group.heading}</p>
              ))}
            {group.items.map(({to, label, Icon, badge}) => {
              const count =
                badge === 'running' ? runningCount : badge === 'approvals' ? approvalCount : 0;
              // Prefix match, matching the router's default non-exact
              // behaviour: /workflow stays lit on /workflow/<id>.
              const active = pathname === to || pathname.startsWith(`${to}/`);
              // A folded group keeps the row you are on, so the rail still
              // says where you are.
              if (folded && !active) return null;
              return (
                <Link
                  key={to}
                  to={to}
                  {...stylex.props(
                    nav.link,
                    active && nav.linkActive,
                    railCollapsed && nav.linkCollapsed,
                  )}
                  title={label}
                >
                  <Icon size={15} style={nav.icon} />
                  {!railCollapsed && <span>{label}</span>}
                  {count > 0 && (
                    <span {...stylex.props(nav.badge, railCollapsed && nav.badgeCompact)}>
                      {count}
                    </span>
                  )}
                </Link>
              );
            })}
          </div>
        );
      })}
    </div>
  );

  return (
    <ToastProvider>
      <div {...stylex.props(shell.appShell)}>
        <aside
          {...stylex.props(
            rail.root,
            railCollapsed && rail.collapsed,
            navResizing && rail.resizing,
            navOpen && rail.open,
          )}
          // Only on desktop: the drawer takes its width from the stylesheet, and
          // an inline width would beat it (which is what the old rule needed an
          // !important to undo).
          style={!railCollapsed && !isMobile ? {width: navWidth} : undefined}
          aria-hidden={isMobile && !navOpen}
          inert={isMobile && !navOpen}
        >
          <div {...stylex.props(rail.header, railCollapsed && rail.headerCollapsed)}>
            <span {...stylex.props(brand.mark)} aria-hidden="true">
              <BrandMark size={railCollapsed ? 18 : 20} />
            </span>
            {!railCollapsed && (
              <div {...stylex.props(brand.block)}>
                <span {...stylex.props(brand.name)}>Jarvis</span>
                {status}
              </div>
            )}
            {!railCollapsed && themeButton(14)}
            {/* On mobile the rail cannot collapse — it is either open or off
                canvas — so the chevron closes the drawer instead. */}
            <button
              {...stylex.props(control.ghost)}
              onClick={isMobile ? () => setNavOpen(false) : toggleNav}
              title={
                isMobile ? 'Close menu' : railCollapsed ? 'Expand sidebar' : 'Collapse sidebar'
              }
              aria-label={
                isMobile ? 'Close menu' : railCollapsed ? 'Expand sidebar' : 'Collapse sidebar'
              }
            >
              <ChevronLeftIcon
                size={14}
                style={[control.chevron, railCollapsed && control.chevronFlipped]}
              />
            </button>
          </div>
          <Link
            to="/"
            {...stylex.props(nav.newChat, railCollapsed && nav.newChatCollapsed)}
            title="New chat"
            onClick={focusComposer}
          >
            <PlusIcon size={15} />
            {!railCollapsed && <span>New chat</span>}
            {!railCollapsed && (
              <kbd {...stylex.props(nav.newChatKbd)}>{IS_MAC ? '⌘K' : 'Ctrl K'}</kbd>
            )}
          </Link>
          {navGroups(PRIMARY_GROUPS)}
          {/* The sidebar renders outside every route, so an unguarded throw
              here blanks the whole app rather than one page. */}
          {!railCollapsed && (
            <QueryBoundary label="Failed to load conversations">
              <ConversationList />
            </QueryBoundary>
          )}
          {navGroups(SECONDARY_GROUPS, true)}
          {!railCollapsed && (
            <div
              {...stylex.props(rail.handle, navResizing && rail.handleActive)}
              onPointerDown={startNavResize}
              onDoubleClick={resetNavWidth}
              title="Drag to resize · double-click to reset"
            />
          )}
        </aside>
        {isMobile && navOpen && (
          <button
            {...stylex.props(mobile.scrim)}
            aria-label="Close menu"
            onClick={() => setNavOpen(false)}
          />
        )}
        <main {...stylex.props(shell.mainPanel)}>
          {/* Always rendered; hidden above the 860px breakpoint. */}
          <header {...stylex.props(mobile.topbar)}>
            <button
              {...stylex.props(control.ghost)}
              onClick={() => setNavOpen(true)}
              aria-label="Open menu"
              aria-expanded={navOpen}
              title="Menu"
            >
              <MenuIcon size={18} />
            </button>
            <span {...stylex.props(brand.mark)} aria-hidden="true">
              <BrandMark size={18} />
            </span>
            <div {...stylex.props(mobile.topbarBrand)}>
              <span {...stylex.props(brand.name, mobile.topbarName)}>Jarvis</span>
              {status}
            </div>
            {themeButton(16)}
          </header>
          {/* Hosts the route so it is a flex child with a definite height in
              both layouts — which is what lets route roots keep `height: 100%`
              whether or not the topbar above is showing. */}
          <div {...stylex.props(shell.outletHost)}>
            <Outlet />
          </div>
        </main>
      </div>
    </ToastProvider>
  );
}

export const Route = createRootRouteWithContext<RouterContext>()({component: RootLayout});
