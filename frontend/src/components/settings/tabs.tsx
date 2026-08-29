import type {ReactNode} from 'react';

/* ── The Settings tab registry ─────────────────────────────────────────
   One entry per child route under `/settings`. The tab is a *route*, not
   component state: it belongs in the URL so a tab can be linked, opened in
   a new window, and survive a reload — and so the strip below can live in
   the `/settings` layout route, where it renders for every child instead
   of being re-mounted by whichever tab happens to be showing. */

export type SettingsTab = 'mcp' | 'tools' | 'notifications' | 'models' | 'config' | 'maintenance';

export interface SettingsTabInfo {
  id: SettingsTab;
  to: string;
  label: string;
  subtitle: ReactNode;
}

export const SETTINGS_TABS = [
  {
    id: 'mcp',
    to: '/settings/mcp',
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
  {
    id: 'tools',
    to: '/settings/tools',
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
  {
    id: 'notifications',
    to: '/settings/notifications',
    label: 'Notifications',
    subtitle: (
      <>
        Define Telegram/Discord delivery targets once, then pick them by name when configuring
        automations or workflows.
      </>
    ),
  },
  {
    id: 'models',
    to: '/settings/models',
    label: 'Models',
    subtitle: (
      <>
        Language models available across providers. Add your own as <code>provider:model_name</code>{' '}
        — no code change needed — and pick which one runs by default. Built-ins are compiled in and
        can only be set as the default.
      </>
    ),
  },
  {
    id: 'config',
    to: '/settings/config',
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
  {
    id: 'maintenance',
    to: '/settings/maintenance',
    label: 'Maintenance',
    subtitle: (
      <>
        Housekeeping that used to need a terminal on the box: prune superseded LangGraph
        checkpoints, and download the Piper voice model that <code>POST /tts</code> needs.
      </>
    ),
  },
] as const satisfies readonly SettingsTabInfo[];

/** The tab a pathname is under. Falls back to the first tab, which is also
 *  where `/settings` itself redirects. */
export function tabFromPathname(pathname: string): SettingsTabInfo {
  return SETTINGS_TABS.find((t) => pathname.startsWith(t.to)) ?? SETTINGS_TABS[0];
}
