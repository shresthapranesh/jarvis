import {createFileRoute} from '@tanstack/react-router';
import {useEffect, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {McpServersQuery as TMcpServersQuery} from '../__generated__/McpServersQuery.graphql';
import type {ModelCatalogQuery as TModelCatalogQuery} from '../__generated__/ModelCatalogQuery.graphql';
import type {NotificationChannelsQuery as TNotificationChannelsQuery} from '../__generated__/NotificationChannelsQuery.graphql';
import {QueryBoundary, useQueryRetry} from '../components/QueryBoundary';
import {McpTab} from '../components/settings/McpTab';
import {ModelsTab} from '../components/settings/ModelsTab';
import {NotificationsTab} from '../components/settings/NotificationsTab';
import {mcpServersQuery} from '../relay/McpServersQuery';
import {modelCatalogQuery} from '../relay/ModelCatalogQuery';
import {notificationChannelsQuery} from '../relay/NotificationChannelsQuery';

export const Route = createFileRoute('/settings')({component: SettingsRoute});

function SettingsRoute() {
  return (
    <QueryBoundary
      label="Failed to load settings"
      fallback={<div className="memory-empty">Loading…</div>}
    >
      <SettingsPage />
    </QueryBoundary>
  );
}

type SettingsTab = 'mcp' | 'notifications' | 'models';

const TAB_INFO: Record<SettingsTab, {label: string; subtitle: React.ReactNode}> = {
  mcp: {
    label: 'MCP servers',
    subtitle: (
      <>
        Connect external tools via the Model Context Protocol. Servers run as subprocesses or HTTP
        endpoints and expose their tools to the agent — either bound to every LLM call, or loaded on
        demand to keep their schemas out of the prompt. Config merges from env{' '}
        <code>JARVIS_MCP_SERVERS</code>, file <code>~/.jarvis/mcp.json</code>, and this UI (which
        wins).
      </>
    ),
  },
  notifications: {
    label: 'Notifications',
    subtitle: (
      <>
        Define Telegram/Discord delivery targets once, then pick them by name when configuring
        automations or workflows.
      </>
    ),
  },
  models: {
    label: 'Models',
    subtitle: (
      <>
        Language models available across providers. Add your own as <code>provider:model_name</code>{' '}
        — no code change needed — and pick which one runs by default. Built-ins are compiled in and
        can only be set as the default.
      </>
    ),
  },
};

function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>(() => {
    const saved = localStorage.getItem('settings-tab') as SettingsTab | null;
    if (saved && ['notifications', 'mcp', 'models'].includes(saved)) return saved;
    return 'mcp';
  });

  useEffect(() => {
    localStorage.setItem('settings-tab', tab);
  }, [tab]);

  // Same three queries the tabs below read; Relay serves them all from one
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

  const counts: Record<SettingsTab, number> = {
    mcp: mcpData.mcpServers.length,
    notifications: channelData.notificationChannels.length,
    models: modelData?.models?.available?.length ?? 0,
  };

  return (
    <div className="page memory-page">
      <header className="memory-header">
        <div>
          <h1>Settings</h1>
          <p className="memory-subtitle">{TAB_INFO[tab].subtitle}</p>
        </div>
      </header>

      <nav className="settings-tabs" aria-label="Settings sections">
        {(Object.keys(TAB_INFO) as SettingsTab[]).map((t) => (
          <button
            key={t}
            className={`settings-tab${tab === t ? ' settings-tab--active' : ''}`}
            onClick={() => setTab(t)}
          >
            {TAB_INFO[t].label}
            <span className="memory-count">{counts[t]}</span>
          </button>
        ))}
      </nav>

      {tab === 'notifications' && <NotificationsTab />}
      {tab === 'mcp' && <McpTab />}
      {tab === 'models' && <ModelsTab />}
    </div>
  );
}
