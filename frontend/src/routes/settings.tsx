import {createFileRoute} from '@tanstack/react-router';
import {useEffect, useMemo, useState} from 'react';
import {graphql, useLazyLoadQuery} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {McpServersQuery as TMcpServersQuery} from '../__generated__/McpServersQuery.graphql';
import type {NotificationChannelsQuery as TNotificationChannelsQuery} from '../__generated__/NotificationChannelsQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import type {NotificationChannelInput} from '../lib/api';
import {commitCreateNotificationChannel} from '../relay/CreateNotificationChannelMutation';
import {commitDeleteNotificationChannel} from '../relay/DeleteNotificationChannelMutation';
import {
  mapChannel,
  notificationChannelsQuery,
  refreshNotificationChannels,
} from '../relay/NotificationChannelsQuery';
import {commitUpdateNotificationChannel} from '../relay/UpdateNotificationChannelMutation';
import {mcpServersQuery, refreshMcpServers} from '../relay/McpServersQuery';
import {environment} from '../relay/environment';
import {useToast} from '../lib/toast';
import {ConfirmDialog} from '../components/ConfirmDialog';
import {FormModal} from '../components/FormModal';
import {QueryBoundary, useQueryRetry} from '../components/QueryBoundary';
import {CheckIcon, EditIcon, PlusIcon, SearchIcon, TrashIcon} from '../components/icons';
import type {
  NotificationChannel,
  NotificationChannelReference,
  NotificationChannelType,
} from '../lib/types';

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
        Connect external tools via the Model Context Protocol. Servers run as subprocesses
        or HTTP endpoints and expose their tools to the agent. Config merges from env{' '}
        <code>JARVIS_MCP_SERVERS</code>, file <code>~/.jarvis/mcp.json</code>, and this UI
        (which wins).
      </>
    ),
  },
  notifications: {
    label: 'Notifications',
    subtitle: (
      <>
        Define Telegram/Discord delivery targets once, then pick them by name when
        configuring automations or workflows.
      </>
    ),
  },
  models: {
    label: 'Models',
    subtitle: (
      <>
        Language models available across providers. Add your own as{' '}
        <code>provider:model_name</code> — no code change needed — and pick which one runs
        by default. Built-ins are compiled in and can only be set as the default.
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
  const channelData = useLazyLoadQuery<TNotificationChannelsQuery>(
    notificationChannelsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const mcpData = useLazyLoadQuery<TMcpServersQuery>(
    mcpServersQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const modelData = useLazyLoadQuery<any>(
    settingsModelsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
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

/* ── Models ───────────────────────────────────────────────────────────── */

const settingsModelsQuery = graphql`
  query settingsModelsQuery {
    models {
      default
      providers
      available {
        id
        label
        provider
        builtin
      }
    }
  }
`;

interface CatalogModel {
  id: string;
  label: string;
  provider: string;
  builtin: boolean;
}

interface ModelCatalogData {
  default: string;
  providers: readonly string[];
  available: readonly CatalogModel[];
}

async function fetchModelCatalog() {
  const res: any = await fetchQuery(environment, settingsModelsQuery, {}, {fetchPolicy: 'network-only'}).toPromise();
  return res?.models as ModelCatalogData | undefined;
}

/**
 * The chat model dropdown (`useModels`) reads this same query from the Relay
 * store, so writing the fresh catalog back into the store updates it too — no
 * cross-cache coordination needed.
 */
function refreshModelCatalog() {
  return fetchModelCatalog().catch(() => undefined);
}

type ModelEditor = {mode: 'add'} | {mode: 'edit'; model: CatalogModel};

function ModelsTab() {
  const toast = useToast();
  const queryData = useLazyLoadQuery<any>(
    settingsModelsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const data = queryData?.models as ModelCatalogData | undefined;

  const [filter, setFilter] = useState('');
  const [providerFilter, setProviderFilter] = useState<string>('all');
  const [editor, setEditor] = useState<ModelEditor | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CatalogModel | null>(null);

  const refresh = refreshModelCatalog;

  const saveMut = useAsyncAction(
    async (draft: {id: string; label: string; provider: string}) => {
      const provider = draft.provider || null;
      const wasEdit = editor?.mode === 'edit';
      if (wasEdit) {
        const {commitUpdateModel} = await import('../relay/UpdateModelMutation');
        await commitUpdateModel(draft.id, draft.label, provider);
      } else {
        const {commitAddModel} = await import('../relay/AddModelMutation');
        await commitAddModel(draft.id, draft.label, provider);
      }
      toast.push(wasEdit ? 'Model updated' : 'Model added', 'success');
      setEditor(null);
      await refresh();
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  const removeMut = useAsyncAction(
    async (id: string) => {
      const {commitRemoveModel} = await import('../relay/RemoveModelMutation');
      await commitRemoveModel(id);
      toast.push('Model removed', 'success');
      setDeleteTarget(null);
      await refresh();
    },
    {
      onError: (e) => {
        setDeleteTarget(null);
        toast.push(e.message, 'error');
      },
    },
  );

  const defaultMut = useAsyncAction(
    async (id: string) => {
      const {commitSetDefaultModel} = await import('../relay/SetDefaultModelMutation');
      await commitSetDefaultModel(id);
      toast.push(`Default model set to ${id}`, 'success');
      await refresh();
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  const providers = useMemo(() => {
    const set = new Set<string>();
    data?.available?.forEach((m) => set.add(m.provider));
    return Array.from(set).sort();
  }, [data]);

  const filtered = useMemo(() => {
    const q = filter.toLowerCase().trim();
    return (data?.available ?? []).filter((m) => {
      if (providerFilter !== 'all' && m.provider !== providerFilter) return false;
      if (!q) return true;
      return (
        m.id.toLowerCase().includes(q) ||
        m.label.toLowerCase().includes(q) ||
        m.provider.toLowerCase().includes(q)
      );
    });
  }, [data, filter, providerFilter]);

  const customCount = (data?.available ?? []).filter((m) => !m.builtin).length;

  return (
    <div className="memory-section">
      <h2 className="memory-section-title">
        Model catalog <span className="memory-count">{data?.available?.length ?? 0}</span>
        <span className="memory-section-hint">
          {customCount} custom · default: <code>{data?.default ?? '—'}</code>
        </span>
        <span className="settings-section-actions">
          <button className="artifact-btn primary" onClick={() => setEditor({mode: 'add'})}>
            <PlusIcon size={14} /> Add model
          </button>
        </span>
      </h2>

      <div className="settings-filter-row">
        <div className="settings-search">
          <SearchIcon size={14} />
          <input
            placeholder="Search models…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <select
          className="auto-form-select settings-filter-select"
          value={providerFilter}
          onChange={(e) => setProviderFilter(e.target.value)}
        >
          <option value="all">All providers</option>
          {providers.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
      </div>

      {filtered.length === 0 ? (
        <div className="memory-empty">No models match the filter.</div>
      ) : (
        <ul className="settings-model-grid">
          {filtered.map((m) => {
            const isDefault = m.id === data?.default;
            return (
              <li key={m.id} className="skill-card settings-model-card">
                <div className="skill-card-head">
                  <span className="settings-badge">{m.provider}</span>
                  {isDefault && (
                    <span className="settings-badge settings-badge--live">default</span>
                  )}
                  {!m.builtin && <span className="settings-badge">custom</span>}
                  {!m.builtin && (
                    <div className="skill-card-controls">
                      <button
                        className="icon-btn"
                        title="Edit model"
                        onClick={() => setEditor({mode: 'edit', model: m})}
                      >
                        <EditIcon size={14} />
                      </button>
                      <button
                        className="icon-btn icon-btn--danger"
                        title="Remove model"
                        onClick={() => setDeleteTarget(m)}
                      >
                        <TrashIcon size={14} />
                      </button>
                    </div>
                  )}
                </div>
                <span className="skill-card-name settings-model-id">{m.id}</span>
                <p className="skill-card-desc">{m.label}</p>
                <div className="settings-model-actions">
                  {isDefault ? (
                    <span className="auto-form-hint">
                      <CheckIcon size={12} /> Used when no model is specified
                    </span>
                  ) : (
                    <button
                      className="artifact-btn small"
                      disabled={defaultMut.pending}
                      onClick={() => void defaultMut.run(m.id)}
                    >
                      Set as default
                    </button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {editor && (
        <ModelModal
          editor={editor}
          providers={data?.providers ?? []}
          pending={saveMut.pending}
          onSubmit={(draft) => void saveMut.run(draft)}
          onClose={() => setEditor(null)}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Remove model"
        message={
          <p>
            Remove <strong>{deleteTarget?.id}</strong> from the catalog? Conversations
            pinned to it fall back to the default model.
            {deleteTarget?.id === data?.default &&
              ' This is the current default — it will reset to the built-in default.'}
          </p>
        }
        confirmLabel="Remove"
        danger
        onConfirm={() => deleteTarget && void removeMut.run(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

function ModelModal({
  editor,
  providers,
  pending,
  onSubmit,
  onClose,
}: {
  editor: ModelEditor;
  providers: readonly string[];
  pending: boolean;
  onSubmit: (draft: {id: string; label: string; provider: string}) => void;
  onClose: () => void;
}) {
  const editing = editor.mode === 'edit';
  const [id, setId] = useState(editing ? editor.model.id : '');
  const [label, setLabel] = useState(editing ? editor.model.label : '');
  // '' means "infer from the id prefix" — the same default the CLI uses.
  const [provider, setProvider] = useState(
    editing && editor.model.provider !== editor.model.id.split(':')[0]
      ? editor.model.provider
      : '',
  );

  const inferred = id.split(':')[0].trim();
  const resolvedProvider = provider || inferred;
  const modelName = id.slice(id.indexOf(':') + 1).trim();

  const error = useMemo(() => {
    if (!id.trim()) return null;
    if (!id.includes(':') || !modelName) {
      return "Expected 'provider:model_name' — e.g. google_genai:gemini-3.5-flash";
    }
    if (!providers.includes(resolvedProvider)) {
      return `Unsupported provider '${resolvedProvider}' — must be one of: ${providers.join(', ')}`;
    }
    return null;
  }, [id, modelName, providers, resolvedProvider]);

  const canSubmit = Boolean(id.trim() && label.trim()) && error === null;

  return (
    <FormModal
      open
      title={editing ? `Edit ${editor.model.id}` : 'Add model'}
      subtitle={
        editing
          ? 'The ID is the catalog key and cannot be changed — remove and re-add to rename.'
          : "Any model from a supported provider works without a code change. The part after ':' is passed verbatim to the provider's SDK."
      }
      submitLabel={editing ? 'Save changes' : 'Add model'}
      submitDisabled={!canSubmit}
      pending={pending}
      error={error}
      onSubmit={() => canSubmit && onSubmit({id: id.trim(), label: label.trim(), provider})}
      onClose={onClose}
    >
      <div className="auto-form-group">
        <span className="auto-form-label">Model ID</span>
        <input
          className="auto-form-input settings-mono"
          placeholder="google_genai:gemini-3.5-flash"
          value={id}
          onChange={(e) => setId(e.target.value)}
          disabled={editing}
          spellCheck={false}
          autoFocus={!editing}
        />
        <span className="auto-form-hint">
          <code>provider:model_name</code> — provider must be one of{' '}
          {providers.join(', ')}.
        </span>
      </div>
      <div className="auto-form-group">
        <span className="auto-form-label">Label</span>
        <input
          className="auto-form-input"
          placeholder="Gemini 3.5 Flash (Google)"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          autoFocus={editing}
        />
        <span className="auto-form-hint">Shown in the model dropdowns.</span>
      </div>
      <div className="auto-form-group">
        <span className="auto-form-label">Provider</span>
        <select
          className="auto-form-select"
          value={provider}
          onChange={(e) => setProvider(e.target.value)}
        >
          <option value="">Infer from ID{inferred ? ` — ${inferred}` : ''}</option>
          {providers.map((p) => (
            <option key={p} value={p}>
              {p}
            </option>
          ))}
        </select>
        <span className="auto-form-hint">
          Which backend builds the client. Override only when the ID prefix isn't the
          provider.
        </span>
      </div>
    </FormModal>
  );
}

/* ── Notifications ────────────────────────────────────────────────────── */

interface ChannelDraft {
  name: string;
  type: NotificationChannelType;
  target: string;
}

const EMPTY_CHANNEL: ChannelDraft = {name: '', type: 'telegram', target: ''};

type ChannelEditor = {mode: 'add'} | {mode: 'edit'; channel: NotificationChannel};

function NotificationsTab() {
  const toast = useToast();
  const channelData = useLazyLoadQuery<TNotificationChannelsQuery>(
    notificationChannelsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const channels = useMemo(
    () => channelData.notificationChannels.map(mapChannel),
    [channelData.notificationChannels],
  );

  const [editor, setEditor] = useState<ChannelEditor | null>(null);
  const [draft, setDraft] = useState<ChannelDraft>(EMPTY_CHANNEL);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<NotificationChannel | null>(null);
  const [refsInUse, setRefsInUse] = useState<NotificationChannelReference[]>([]);

  const invalidate = refreshNotificationChannels;

  function openAdd() {
    setDraft(EMPTY_CHANNEL);
    setActionError(null);
    setEditor({mode: 'add'});
  }

  function openEdit(ch: NotificationChannel) {
    setDraft({name: ch.name, type: ch.type, target: ch.target});
    setActionError(null);
    setEditor({mode: 'edit', channel: ch});
  }

  function closeEditor() {
    setEditor(null);
    setActionError(null);
  }

  const input: NotificationChannelInput = {
    name: draft.name.trim(),
    type: draft.type,
    target: draft.target.trim(),
  };

  const createMut = useAsyncAction(
    async () => {
      await commitCreateNotificationChannel(input);
      toast.push('Channel created', 'success');
      await invalidate();
    },
    {onSuccess: closeEditor, onError: (e) => setActionError(e.message)},
  );

  const updateMut = useAsyncAction(
    async (id: string) => {
      await commitUpdateNotificationChannel(id, input);
      toast.push('Channel updated', 'success');
      await invalidate();
    },
    {onSuccess: closeEditor, onError: (e) => setActionError(e.message)},
  );

  const deleteMut = useAsyncAction(
    async (id: string) => {
      const result = await commitDeleteNotificationChannel(id);
      setDeleteTarget(null);
      if (result.ok) {
        toast.push('Channel deleted', 'success');
        setRefsInUse([]);
        await invalidate();
      } else {
        // Still referenced by an automation/workflow — show what blocks it.
        setRefsInUse(result.references ?? []);
      }
    },
    {
      onError: (e) => {
        setDeleteTarget(null);
        toast.push(e.message, 'error');
      },
  });

  const draftValid = Boolean(draft.name.trim() && draft.target.trim());

  return (
    <div className="memory-section">
      <h2 className="memory-section-title">
        Channels <span className="memory-count">{channels.length}</span>
        <span className="memory-section-hint">
          chat IDs from @userinfobot (Telegram) or Developer Mode → Copy ID (Discord)
        </span>
        <span className="settings-section-actions">
          <button className="artifact-btn primary" onClick={openAdd}>
            <PlusIcon size={14} /> New channel
          </button>
        </span>
      </h2>

      {refsInUse.length > 0 && (
        <div className="memory-error">
          Cannot delete — still used by:{' '}
          {refsInUse.map((r, i) => (
            <span key={`${r.kind}:${r.id}`}>
              {i > 0 && ', '}
              {r.kind} "{r.name}"
            </span>
          ))}
        </div>
      )}

      {channels.length === 0 ? (
        <div className="memory-empty">
          <p>No channels yet.</p>
          <p>Create one to get notified when automations finish, fail, or detect changes.</p>
          <button className="artifact-btn primary" onClick={openAdd}>
            <PlusIcon size={14} /> New channel
          </button>
        </div>
      ) : (
        <ul className="memory-list">
          {channels.map((ch) => (
            <li key={ch.id} className="memory-item">
              <div className="memory-item-main">
                <span className="memory-item-text settings-channel-name">
                  <span
                    className={`settings-badge settings-badge--${ch.type === 'telegram' ? 'http' : 'sse'}`}
                  >
                    {ch.type}
                  </span>
                  {ch.name}
                </span>
                <span className="memory-item-meta">target: {ch.target}</span>
              </div>
              <div className="memory-item-actions">
                <button className="icon-btn" title="Edit channel" onClick={() => openEdit(ch)}>
                  <EditIcon size={14} />
                </button>
                <button
                  className="icon-btn icon-btn--danger"
                  title="Delete channel"
                  onClick={() => setDeleteTarget(ch)}
                >
                  <TrashIcon size={14} />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <FormModal
        open={editor !== null}
        title={editor?.mode === 'edit' ? 'Edit channel' : 'New channel'}
        subtitle="Referenced by name from automations and workflows."
        submitLabel={editor?.mode === 'edit' ? 'Save changes' : 'Create channel'}
        submitDisabled={!draftValid}
        pending={createMut.pending || updateMut.pending}
        error={actionError}
        onSubmit={() => {
          if (!editor) return;
          if (editor.mode === 'add') void createMut.run();
          else void updateMut.run(editor.channel.id);
        }}
        onClose={closeEditor}
      >
        <div className="auto-form-group">
          <span className="auto-form-label">Name</span>
          <input
            className="auto-form-input"
            value={draft.name}
            onChange={(e) => setDraft({...draft, name: e.target.value})}
            autoFocus={editor?.mode === 'add'}
            spellCheck={false}
            placeholder="team-discord"
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Type</span>
          <select
            className="auto-form-select"
            value={draft.type}
            onChange={(e) =>
              setDraft({...draft, type: e.target.value as NotificationChannelType})
            }
          >
            <option value="telegram">Telegram</option>
            <option value="discord">Discord</option>
          </select>
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">
            Target — {draft.type === 'telegram' ? 'chat ID' : 'channel ID'}
          </span>
          <input
            className="auto-form-input"
            value={draft.target}
            onChange={(e) => setDraft({...draft, target: e.target.value})}
            spellCheck={false}
            placeholder={draft.type === 'telegram' ? '123456789' : '123456789012345678'}
          />
        </div>
      </FormModal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete channel"
        message={
          <p>
            Delete <strong>{deleteTarget?.name}</strong>? Automations referencing it will
            stop delivering.
          </p>
        }
        confirmLabel="Delete"
        danger
        onConfirm={() => deleteTarget && void deleteMut.run(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

/* ── MCP servers ──────────────────────────────────────────────────────── */

type McpTransport = 'stdio' | 'http' | 'sse' | 'streamable-http';

interface McpFormState {
  name: string;
  transport: McpTransport;
  command: string;
  args: string[];
  env: {k: string; v: string}[];
  url: string;
  headers: {k: string; v: string}[];
  advancedJson: string;
  useAdvanced: boolean;
}

interface McpPreset {
  id: string;
  name: string;
  desc: string;
  config: any;
}

const MCP_PRESETS: McpPreset[] = [
  {id: 'filesystem', name: 'Filesystem', desc: 'Read/write local files', config: {command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'], transport: 'stdio'}},
  {id: 'brave', name: 'Brave Search', desc: 'Web search via Brave', config: {command: 'npx', args: ['-y', '@modelcontextprotocol/server-brave-search'], transport: 'stdio', env: {BRAVE_API_KEY: ''}}},
  {id: 'github', name: 'GitHub', desc: 'Repos, issues, PRs', config: {command: 'npx', args: ['-y', '@modelcontextprotocol/server-github'], transport: 'stdio', env: {GITHUB_PERSONAL_ACCESS_TOKEN: ''}}},
  {id: 'postgres', name: 'Postgres', desc: 'Query PostgreSQL', config: {command: 'npx', args: ['-y', '@modelcontextprotocol/server-postgres', 'postgresql://user:pass@localhost/db'], transport: 'stdio'}},
  {id: 'fetch', name: 'Fetch', desc: 'HTTP fetch & extract', config: {command: 'npx', args: ['-y', '@modelcontextprotocol/server-fetch'], transport: 'stdio'}},
  {id: 'puppeteer', name: 'Puppeteer', desc: 'Headless browser', config: {command: 'npx', args: ['-y', '@modelcontextprotocol/server-puppeteer'], transport: 'stdio'}},
  {id: 'memory', name: 'Memory', desc: 'Knowledge graph', config: {command: 'npx', args: ['-y', '@modelcontextprotocol/server-memory'], transport: 'stdio'}},
  {id: 'sqlite', name: 'SQLite', desc: 'Local SQLite DB', config: {command: 'npx', args: ['-y', '@modelcontextprotocol/server-sqlite', '--db-path', '/tmp/db.sqlite'], transport: 'stdio'}},
];

function configToForm(name: string, rawJson: string): McpFormState {
  let cfg: any = {};
  try {
    cfg = JSON.parse(rawJson || '{}');
  } catch {
    cfg = {};
  }
  const transport = (cfg.transport as McpTransport) || (cfg.url ? 'http' : 'stdio');
  let args: string[] = [];
  if (Array.isArray(cfg.args)) args = cfg.args;
  else if (typeof cfg.args === 'string') args = [cfg.args];
  const envObj = cfg.env && typeof cfg.env === 'object' ? cfg.env : {};
  const headersObj = cfg.headers && typeof cfg.headers === 'object' ? cfg.headers : {};
  return {
    name: name || '',
    transport,
    command: cfg.command ? (Array.isArray(cfg.command) ? cfg.command.join(' ') : cfg.command) : 'npx',
    args,
    env: Object.entries(envObj).map(([k, v]) => ({k, v: String(v)})),
    url: cfg.url || '',
    headers: Object.entries(headersObj).map(([k, v]) => ({k, v: String(v)})),
    advancedJson: (() => {
      try {
        return JSON.stringify(cfg, null, 2);
      } catch {
        return rawJson;
      }
    })(),
    useAdvanced: false,
  };
}

function formToConfigJson(form: McpFormState): string {
  if (form.useAdvanced) return form.advancedJson;
  const out: any = {};
  out.transport = form.transport;
  if (form.transport === 'stdio') {
    const cmd = form.command.trim().split(/\s+/).filter(Boolean);
    const extraArgs = form.args.filter((a) => a.trim());
    if (cmd.length >= 1) out.command = cmd[0];
    const allArgs = [...cmd.slice(1), ...extraArgs];
    if (allArgs.length) out.args = allArgs;
    const env: Record<string, string> = {};
    form.env.forEach(({k, v}) => {
      if (k.trim()) env[k.trim()] = v;
    });
    if (Object.keys(env).length) out.env = env;
  } else {
    out.url = form.url.trim();
    const headers: Record<string, string> = {};
    form.headers.forEach(({k, v}) => {
      if (k.trim()) headers[k.trim()] = v;
    });
    if (Object.keys(headers).length) out.headers = headers;
  }
  return JSON.stringify(out);
}

function prettyJson(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function McpTab() {
  const toast = useToast();
  const data = useLazyLoadQuery<TMcpServersQuery>(
    mcpServersQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );

  const [showAdd, setShowAdd] = useState(false);
  const [editing, setEditing] = useState<{name: string; config: string} | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const servers = data.mcpServers;
  const tools = data.mcpTools;

  const refresh = refreshMcpServers;

  const addMut = useAsyncAction(
    async (form: McpFormState) => {
      const json = formToConfigJson(form);
      JSON.parse(json); // validate before sending
      const {commitAddMcpServer} = await import('../relay/AddMcpServerMutation');
      await commitAddMcpServer(form.name.trim(), json);
      toast.push('MCP server added', 'success');
      setShowAdd(false);
      await refresh();
    },
    {onError: (e) => toast.push(e.message || String(e), 'error')},
  );

  const updateMut = useAsyncAction(
    async (form: McpFormState) => {
      if (!editing) return;
      const json = formToConfigJson(form);
      JSON.parse(json);
      const {commitUpdateMcpServer} = await import('../relay/UpdateMcpServerMutation');
      await commitUpdateMcpServer(editing.name, json);
      toast.push('MCP server updated', 'success');
      setEditing(null);
      await refresh();
    },
    {onError: (e) => toast.push(e.message || String(e), 'error')},
  );

  const removeMut = useAsyncAction(
    async (name: string) => {
      const {commitRemoveMcpServer} = await import('../relay/RemoveMcpServerMutation');
      await commitRemoveMcpServer(name);
      toast.push('MCP server removed', 'success');
      setDeleteTarget(null);
      await refresh();
    },
    {
      onError: (e) => {
        setDeleteTarget(null);
        toast.push(e.message || String(e), 'error');
      },
    },
  );

  const reloadMut = useAsyncAction(
    async () => {
      const {commitReloadMcpServers} = await import('../relay/ReloadMcpServersMutation');
      await commitReloadMcpServers();
      toast.push('MCP reloaded', 'success');
      await refresh();
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  function toolsForServer(server: {name: string}): string[] {
    if (servers.length === 1) return [...tools];
    const matching = tools.filter((t) => t.toLowerCase().includes(server.name.toLowerCase()));
    return matching.length ? matching : [];
  }

  return (
    <div className="memory-section">
      <h2 className="memory-section-title">
        Servers <span className="memory-count">{servers.length}</span>
        <span className="memory-section-hint">{tools.length} tools loaded</span>
        <span className="settings-section-actions">
          <button
            className="artifact-btn"
            onClick={() => void reloadMut.run()}
            disabled={reloadMut.pending}
            title="Reload MCP connections"
          >
            {reloadMut.pending ? 'Reloading…' : 'Reload'}
          </button>
          <button className="artifact-btn primary" onClick={() => setShowAdd(true)}>
            <PlusIcon size={14} /> Add server
          </button>
        </span>
      </h2>

      {servers.length === 0 ? (
        <div className="memory-empty">
          <p>No MCP servers configured.</p>
          <p>
            MCP servers extend the agent with external tools — start from a preset or add
            your own stdio/HTTP server.
          </p>
          <button className="artifact-btn primary" onClick={() => setShowAdd(true)}>
            <PlusIcon size={14} /> Add your first server
          </button>
        </div>
      ) : (
        <ul className="memory-list">
          {servers.map((s: any) => {
            const serverTools = toolsForServer(s);
            return (
              <li key={s.name} className="skill-card">
                <div className="skill-card-head">
                  <span className="skill-card-name">{s.name}</span>
                  <span className={`settings-badge settings-badge--${s.transport === 'stdio' ? 'stdio' : s.transport === 'http' ? 'http' : 'sse'}`}>
                    {s.transport}
                  </span>
                  {s.toolCount > 0 ? (
                    <span className="settings-badge settings-badge--live">
                      {s.toolCount} tools
                    </span>
                  ) : (
                    <span className="settings-badge">not loaded</span>
                  )}
                  <div className="skill-card-controls">
                    <button
                      className="icon-btn"
                      title="Edit server"
                      onClick={() => setEditing({name: s.name, config: s.config})}
                    >
                      <EditIcon size={14} />
                    </button>
                    <button
                      className="icon-btn icon-btn--danger"
                      title="Remove server"
                      onClick={() => setDeleteTarget(s.name)}
                    >
                      <TrashIcon size={14} />
                    </button>
                  </div>
                </div>
                <p className="skill-card-desc settings-mono">{s.command || s.url || 'custom config'}</p>
                <details className="skill-card-body">
                  <summary>Configuration{serverTools.length > 0 ? ' & tools' : ''}</summary>
                  {serverTools.length > 0 && (
                    <div className="settings-tool-chips">
                      {serverTools.map((t) => (
                        <span key={t} className="settings-badge">{t}</span>
                      ))}
                    </div>
                  )}
                  {s.toolCount === 0 && (
                    <p className="memory-section-empty">
                      Server not loaded — tools appear after a successful connection. Try
                      Reload after adding.
                    </p>
                  )}
                  <pre>{prettyJson(s.config)}</pre>
                  <button
                    className="artifact-btn small"
                    onClick={() => {
                      navigator.clipboard.writeText(s.config);
                      toast.push('Config copied', 'success');
                    }}
                  >
                    Copy JSON
                  </button>
                </details>
              </li>
            );
          })}
        </ul>
      )}

      {showAdd && (
        <McpServerModal
          title="Add MCP server"
          initial={null}
          onClose={() => setShowAdd(false)}
          onSubmit={(f) => void addMut.run(f)}
          submitting={addMut.pending}
        />
      )}

      {editing && (
        <McpServerModal
          title={`Edit ${editing.name}`}
          initial={editing}
          onClose={() => setEditing(null)}
          onSubmit={(f) => void updateMut.run(f)}
          submitting={updateMut.pending}
        />
      )}

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Remove MCP server"
        message={
          <p>
            Remove <strong>{deleteTarget}</strong>? This deletes the DB override — env/file
            sources remain.
          </p>
        }
        confirmLabel="Remove"
        danger
        onConfirm={() => deleteTarget && void removeMut.run(deleteTarget)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}

function McpServerModal({
  title,
  initial,
  onClose,
  onSubmit,
  submitting,
}: {
  title: string;
  initial: {name: string; config: string} | null;
  onClose: () => void;
  onSubmit: (f: McpFormState) => void;
  submitting: boolean;
}) {
  const [form, setForm] = useState<McpFormState>(() => {
    if (initial) return configToForm(initial.name, initial.config);
    return {
      name: '',
      transport: 'stdio',
      command: 'npx',
      args: ['-y', '@modelcontextprotocol/server-filesystem', '/tmp'],
      env: [],
      url: '',
      headers: [],
      advancedJson:
        '{\n  "command": "npx",\n  "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],\n  "transport": "stdio"\n}',
      useAdvanced: false,
    };
  });

  function applyPreset(p: McpPreset) {
    const cfg = p.config;
    const args = Array.isArray(cfg.args) ? cfg.args : [];
    const env = cfg.env ? Object.entries(cfg.env).map(([k, v]) => ({k, v: String(v)})) : [];
    setForm((f) => ({
      ...f,
      name: f.name || p.id,
      transport: (cfg.transport as McpTransport) || 'stdio',
      command: cfg.command || f.command,
      args: args.length ? args : f.args,
      env: env.length ? env : f.env,
      advancedJson: JSON.stringify(cfg, null, 2),
    }));
  }

  function update<K extends keyof McpFormState>(key: K, val: McpFormState[K]) {
    setForm((f) => ({...f, [key]: val}));
  }

  function updateList(key: 'env' | 'headers', i: number, field: 'k' | 'v', val: string) {
    setForm((f) => {
      const list = [...f[key]];
      list[i] = {...list[i], [field]: val};
      return {...f, [key]: list};
    });
  }

  const advancedJsonError = useMemo(() => {
    if (!form.useAdvanced) return null;
    try {
      JSON.parse(form.advancedJson);
      return null;
    } catch (err: any) {
      return String(err.message || err);
    }
  }, [form.useAdvanced, form.advancedJson]);

  const canSubmit = useMemo(() => {
    if (!form.name.trim()) return false;
    if (form.useAdvanced) return advancedJsonError === null;
    if (form.transport === 'stdio') return !!form.command.trim();
    return !!form.url.trim();
  }, [form, advancedJsonError]);

  return (
    <FormModal
      open
      title={title}
      subtitle={
        initial
          ? 'Name cannot be changed when editing — delete & recreate to rename.'
          : 'Pick a preset or configure a stdio/HTTP server by hand.'
      }
      wide
      submitLabel={initial ? 'Save changes' : 'Add server'}
      submitDisabled={!canSubmit}
      pending={submitting}
      error={advancedJsonError}
      footerExtra={
        <label className="switch switch--labeled">
          <input
            type="checkbox"
            checked={form.useAdvanced}
            onChange={(e) => update('useAdvanced', e.target.checked)}
          />
          <span className="switch-track" aria-hidden="true" />
          Raw JSON
        </label>
      }
      onSubmit={() => canSubmit && onSubmit(form)}
      onClose={onClose}
    >
      {!initial && (
        <div className="auto-form-group">
          <span className="auto-form-label">Quick presets</span>
          <div className="settings-preset-strip">
            {MCP_PRESETS.map((p) => (
              <button key={p.id} type="button" className="settings-preset" onClick={() => applyPreset(p)}>
                <span className="settings-preset-name">{p.name}</span>
                <span className="settings-preset-desc">{p.desc}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="settings-form-row">
        <div className="auto-form-group settings-form-grow">
          <span className="auto-form-label">Name</span>
          <input
            className="auto-form-input settings-mono"
            placeholder="e.g. filesystem"
            value={form.name}
            onChange={(e) => update('name', e.target.value)}
            disabled={!!initial}
            spellCheck={false}
            autoFocus={!initial}
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Transport</span>
          <select
            className="auto-form-select"
            value={form.transport}
            onChange={(e) => update('transport', e.target.value as McpTransport)}
            disabled={form.useAdvanced}
          >
            <option value="stdio">stdio</option>
            <option value="http">http</option>
            <option value="sse">sse</option>
            <option value="streamable-http">streamable-http</option>
          </select>
        </div>
      </div>

      {!form.useAdvanced && form.transport === 'stdio' && (
        <>
          <div className="auto-form-group">
            <span className="auto-form-label">Command</span>
            <input
              className="auto-form-input settings-mono"
              placeholder="npx or python or /path/to/binary"
              value={form.command}
              onChange={(e) => update('command', e.target.value)}
              spellCheck={false}
            />
          </div>

          <div className="auto-form-group">
            <span className="auto-form-label">Arguments</span>
            {form.args.map((a, i) => (
              <div key={i} className="settings-kv-row">
                <input
                  className="auto-form-input settings-mono"
                  value={a}
                  onChange={(e) =>
                    setForm((f) => {
                      const args = [...f.args];
                      args[i] = e.target.value;
                      return {...f, args};
                    })
                  }
                  placeholder={`arg ${i + 1}`}
                  spellCheck={false}
                />
                <button
                  type="button"
                  className="icon-btn"
                  title="Remove argument"
                  onClick={() => setForm((f) => ({...f, args: f.args.filter((_, idx) => idx !== i)}))}
                >
                  <TrashIcon size={13} />
                </button>
              </div>
            ))}
            <button
              type="button"
              className="artifact-btn small settings-add-row-btn"
              onClick={() => setForm((f) => ({...f, args: [...f.args, '']}))}
            >
              <PlusIcon size={12} /> Add argument
            </button>
          </div>

          <div className="auto-form-group">
            <span className="auto-form-label">Environment variables</span>
            {form.env.map((pair, i) => (
              <div key={i} className="settings-kv-row">
                <input
                  className="auto-form-input settings-mono settings-kv-key"
                  value={pair.k}
                  onChange={(e) => updateList('env', i, 'k', e.target.value)}
                  placeholder="KEY"
                  spellCheck={false}
                />
                <input
                  className="auto-form-input settings-mono"
                  value={pair.v}
                  onChange={(e) => updateList('env', i, 'v', e.target.value)}
                  placeholder="value"
                  spellCheck={false}
                />
                <button
                  type="button"
                  className="icon-btn"
                  title="Remove variable"
                  onClick={() => setForm((f) => ({...f, env: f.env.filter((_, idx) => idx !== i)}))}
                >
                  <TrashIcon size={13} />
                </button>
              </div>
            ))}
            {form.env.length === 0 && (
              <span className="auto-form-hint">Secrets like API keys go here.</span>
            )}
            <button
              type="button"
              className="artifact-btn small settings-add-row-btn"
              onClick={() => setForm((f) => ({...f, env: [...f.env, {k: '', v: ''}]}))}
            >
              <PlusIcon size={12} /> Add variable
            </button>
          </div>
        </>
      )}

      {!form.useAdvanced && form.transport !== 'stdio' && (
        <>
          <div className="auto-form-group">
            <span className="auto-form-label">URL</span>
            <input
              className="auto-form-input settings-mono"
              placeholder="https://example.com/mcp"
              value={form.url}
              onChange={(e) => update('url', e.target.value)}
              spellCheck={false}
            />
          </div>
          <div className="auto-form-group">
            <span className="auto-form-label">Headers</span>
            {form.headers.map((pair, i) => (
              <div key={i} className="settings-kv-row">
                <input
                  className="auto-form-input settings-mono settings-kv-key"
                  value={pair.k}
                  onChange={(e) => updateList('headers', i, 'k', e.target.value)}
                  placeholder="Authorization"
                  spellCheck={false}
                />
                <input
                  className="auto-form-input settings-mono"
                  value={pair.v}
                  onChange={(e) => updateList('headers', i, 'v', e.target.value)}
                  placeholder="Bearer …"
                  spellCheck={false}
                />
                <button
                  type="button"
                  className="icon-btn"
                  title="Remove header"
                  onClick={() =>
                    setForm((f) => ({...f, headers: f.headers.filter((_, idx) => idx !== i)}))
                  }
                >
                  <TrashIcon size={13} />
                </button>
              </div>
            ))}
            {form.headers.length === 0 && (
              <span className="auto-form-hint">Optional — auth tokens etc.</span>
            )}
            <button
              type="button"
              className="artifact-btn small settings-add-row-btn"
              onClick={() => setForm((f) => ({...f, headers: [...f.headers, {k: '', v: ''}]}))}
            >
              <PlusIcon size={12} /> Add header
            </button>
          </div>
        </>
      )}

      {form.useAdvanced ? (
        <div className="auto-form-group">
          <span className="auto-form-label">Raw config JSON</span>
          <textarea
            className="auto-form-textarea auto-form-code"
            rows={8}
            value={form.advancedJson}
            onChange={(e) => update('advancedJson', e.target.value)}
            spellCheck={false}
          />
        </div>
      ) : (
        <div className="auto-form-group">
          <span className="auto-form-label">Preview JSON</span>
          <pre className="settings-config-pre">{prettyJson(formToConfigJson(form))}</pre>
        </div>
      )}
    </FormModal>
  );
}
