import {createFileRoute} from '@tanstack/react-router';
import {useEffect, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {McpServersQuery as TMcpServersQuery} from '../__generated__/McpServersQuery.graphql';
import type {ModelCatalogQuery as TModelCatalogQuery} from '../__generated__/ModelCatalogQuery.graphql';
import type {NotificationChannelsQuery as TNotificationChannelsQuery} from '../__generated__/NotificationChannelsQuery.graphql';
import type {SettingsQuery as TSettingsQuery} from '../__generated__/SettingsQuery.graphql';
import type {ToolsQuery as TToolsQuery} from '../__generated__/ToolsQuery.graphql';
import {QueryBoundary, useQueryRetry} from '../components/QueryBoundary';
import {ConfigTab} from '../components/settings/ConfigTab';
import {MaintenanceTab} from '../components/settings/MaintenanceTab';
import {McpTab} from '../components/settings/McpTab';
import {ModelsTab} from '../components/settings/ModelsTab';
import {NotificationsTab} from '../components/settings/NotificationsTab';
import {ToolsTab} from '../components/settings/ToolsTab';
import {mcpServersQuery} from '../relay/McpServersQuery';
import {modelCatalogQuery} from '../relay/ModelCatalogQuery';
import {notificationChannelsQuery} from '../relay/NotificationChannelsQuery';
import {settingsQuery} from '../relay/SettingsQuery';
import {toolsQuery} from '../relay/ToolsQuery';

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

type SettingsTab = 'mcp' | 'tools' | 'notifications' | 'models' | 'config' | 'maintenance';

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
  tools: {
    label: 'Tools',
    subtitle: (
      <>
        Every tool the agent can reach — the ones bound to its graph, the <code>jarvis</code> SDK it
        calls from <code>run_cell</code>, and each MCP server&apos;s tools. Switch one off to remove
        it entirely, or require approval: a gated call blocks until you answer it here or in{' '}
        <code>/approvals</code>, on every surface including automations and board tasks.
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
  config: {
    label: 'Config',
    subtitle: (
      <>
        The <code>config_settings</code> table — bot allowlists, the embedding model, the scheduler
        timezone, which agent actions need approval. The same rows{' '}
        <code>main.py config set/get/list/delete</code> writes, except that a write here is also
        pushed into the running server instead of waiting for a restart. Keys another tab owns are
        shown read-only.
      </>
    ),
  },
  maintenance: {
    label: 'Maintenance',
    subtitle: (
      <>
        Housekeeping that used to need a terminal on the box: prune superseded LangGraph
        checkpoints, and download the Piper voice model that <code>POST /tts</code> needs.
      </>
    ),
  },
};

function SettingsPage() {
  const [tab, setTab] = useState<SettingsTab>(() => {
    const saved = localStorage.getItem('settings-tab') as SettingsTab | null;
    if (saved && saved in TAB_INFO) return saved;
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
    config: settingData.settings.filter((s) => s.isSet).length,
    maintenance: null,
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
            {counts[t] !== null && <span className="memory-count">{counts[t]}</span>}
          </button>
        ))}
      </nav>

      {tab === 'notifications' && <NotificationsTab />}
      {tab === 'mcp' && <McpTab />}
      {tab === 'tools' && <ToolsTab />}
      {tab === 'models' && <ModelsTab />}
      {tab === 'config' && <ConfigTab />}
      {tab === 'maintenance' && <MaintenanceTab />}
    </div>
  );
}
