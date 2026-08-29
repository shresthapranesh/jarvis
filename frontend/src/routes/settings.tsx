import * as stylex from '@stylexjs/stylex';
import {createFileRoute, Link, Outlet, useLocation} from '@tanstack/react-router';
import {useLazyLoadQuery} from 'react-relay';

import type {McpServersQuery as TMcpServersQuery} from '../__generated__/McpServersQuery.graphql';
import type {ModelCatalogQuery as TModelCatalogQuery} from '../__generated__/ModelCatalogQuery.graphql';
import type {NotificationChannelsQuery as TNotificationChannelsQuery} from '../__generated__/NotificationChannelsQuery.graphql';
import type {SettingsQuery as TSettingsQuery} from '../__generated__/SettingsQuery.graphql';
import type {ToolsQuery as TToolsQuery} from '../__generated__/ToolsQuery.graphql';
import {QueryBoundary, useQueryRetry} from '../components/QueryBoundary';
import {settings} from '../components/settings/settings.styles';
import type {SettingsTab} from '../components/settings/tabs';
import {SETTINGS_TABS, tabFromPathname} from '../components/settings/tabs';
import {page} from '../components/ui';
import {mcpServersQuery} from '../relay/McpServersQuery';
import {modelCatalogQuery} from '../relay/ModelCatalogQuery';
import {notificationChannelsQuery} from '../relay/NotificationChannelsQuery';
import {settingsQuery} from '../relay/SettingsQuery';
import {toolsQuery} from '../relay/ToolsQuery';

// A layout route: `/settings` renders the chrome — title, tab strip, the
// active tab's blurb — and each tab is a child route below it. The strip
// therefore belongs to the layout, so it is on screen for every tab and
// cannot be re-mounted (or omitted) by whichever tab is showing.
export const Route = createFileRoute('/settings')({component: SettingsRoute});

function SettingsRoute() {
  return (
    <QueryBoundary
      label="Failed to load settings"
      fallback={<div {...stylex.props(page.empty)}>Loading…</div>}
    >
      <SettingsLayout />
    </QueryBoundary>
  );
}

function SettingsLayout() {
  const active = tabFromPathname(useLocation().pathname);

  // The same five queries the tabs below read; Relay serves them all from one
  // store, so the counts cost no extra round trips.
  const retry = useQueryRetry();
  const channelData = useLazyLoadQuery<TNotificationChannelsQuery>(
    notificationChannelsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: retry},
  );
  const mcpData = useLazyLoadQuery<TMcpServersQuery>(
    mcpServersQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: retry},
  );
  const modelData = useLazyLoadQuery<TModelCatalogQuery>(
    modelCatalogQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: retry},
  );
  const toolData = useLazyLoadQuery<TToolsQuery>(
    toolsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: retry},
  );
  const settingData = useLazyLoadQuery<TSettingsQuery>(
    settingsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: retry},
  );

  // null = nothing countable; the tab renders without a badge. Maintenance is
  // two actions, not a list, so a number there would be noise.
  const counts: Record<SettingsTab, number | null> = {
    mcp: mcpData.mcpServers.length,
    tools: toolData.tools.length,
    notifications: channelData.notificationChannels.length,
    models: modelData?.models?.available?.length ?? 0,
    // Every other badge is "how many rows this tab lists", and so is the
    // Config section heading — counting only the set ones here made the tab
    // say 4 next to a heading saying 12. The set count stays in that
    // heading's hint, which is where it means something.
    config: settingData.settings.length,
    maintenance: null,
  };

  return (
    <div {...stylex.props(page.scroll, settings.root)}>
      {/* Title only. The per-tab blurb sits *below* the strip: it varies in
          height between tabs, and above the strip that made the tabs jump
          under the cursor on every switch. */}
      <header {...stylex.props(page.header, settings.chromeTop)}>
        <div {...stylex.props(page.headerMain)}>
          <h1 {...stylex.props(page.title)}>Settings</h1>
        </div>
      </header>

      <nav {...stylex.props(settings.tabs)} aria-label="Settings sections">
        {SETTINGS_TABS.map((t) => (
          <Link
            key={t.id}
            to={t.to}
            {...stylex.props(settings.tab, t.id === active.id && settings.tabActive)}
          >
            {t.label}
            {counts[t.id] !== null && <span {...stylex.props(page.count)}>{counts[t.id]}</span>}
          </Link>
        ))}
      </nav>

      <p {...stylex.props(page.subtitle)}>{active.subtitle}</p>

      <Outlet />
    </div>
  );
}
