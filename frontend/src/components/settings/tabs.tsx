import type {ReactNode} from 'react';

/* ── The Settings tab registry ─────────────────────────────────────────
   One entry per child route under `/settings`. The tab is a *route*, not
   component state: it belongs in the URL so a tab can be linked, opened in
   a new window, and survive a reload — and so the strip below can live in
   the `/settings` layout route, where it renders for every child instead
   of being re-mounted by whichever tab happens to be showing. */

export type SettingsTab = 'mcp' | 'tools' | 'notifications' | 'models' | 'config';

export interface SettingsTabInfo {
  id: SettingsTab;
  to: string;
  label: string;
  subtitle: ReactNode;
}

// Ordered by when a new install needs them: a model to run on, somewhere to be
// told, then what the agent may touch.
export const SETTINGS_TABS = [
  {
    id: 'models',
    to: '/settings/models',
    label: 'Models',
    subtitle: (
      <>
        The models Jarvis can run on, and which one it uses by default. Add any other as{' '}
        <code>provider:model_name</code>. Built-in models can&apos;t be edited, only chosen as the
        default.
      </>
    ),
  },
  {
    id: 'notifications',
    to: '/settings/notifications',
    label: 'Notifications',
    subtitle: (
      <>
        Set up Telegram or Discord destinations once, then pick them by name in an automation or
        workflow.
      </>
    ),
  },
  {
    id: 'tools',
    to: '/settings/tools',
    label: 'Tools',
    subtitle: (
      <>
        Everything the agent can use: its built-in tools, the Python helpers it calls from its
        notebook, and tools from MCP servers. Turn one off to take it away, or tick Ask first to
        make every call wait for your approval — here or on the Approvals page, including in
        automations and board tasks.
      </>
    ),
  },
  {
    id: 'mcp',
    to: '/settings/mcp',
    label: 'MCP servers',
    subtitle: (
      <>
        Connect outside tools through MCP (Model Context Protocol) servers. A server can load its
        tools into every request, or only when the agent asks for them, which keeps requests
        smaller. Servers can also come from the <code>JARVIS_MCP_SERVERS</code> environment variable
        or <code>~/.jarvis/mcp.json</code>; one defined here wins.
      </>
    ),
  },
  {
    id: 'config',
    to: '/settings/config',
    label: 'Config',
    subtitle: (
      <>
        Advanced settings: who may use the bots, the embedding model, the scheduler&apos;s timezone,
        which agent actions need approval. They are the values <code>jarvis-edge config</code> reads
        and writes, and a change here applies at once unless it is marked as needing a restart.
        Settings another tab manages are read-only here.
      </>
    ),
  },
] as const satisfies readonly SettingsTabInfo[];

/** The tab a pathname is under. Falls back to the first tab, which is also
 *  where `/settings` itself redirects. */
export function tabFromPathname(pathname: string): SettingsTabInfo {
  return SETTINGS_TABS.find((t) => pathname.startsWith(t.to)) ?? SETTINGS_TABS[0];
}
