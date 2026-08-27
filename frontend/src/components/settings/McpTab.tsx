import * as stylex from '@stylexjs/stylex';
import {useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {McpServersQuery as TMcpServersQuery} from '../../__generated__/McpServersQuery.graphql';
import {useAsyncAction} from '../../hooks/useAsyncAction';
import {useToast} from '../../lib/toast';
import {mcpServersQuery, refreshMcpServers} from '../../relay/McpServersQuery';
import {ConfirmDialog} from '../ConfirmDialog';
import {EditIcon, PlusIcon, TrashIcon} from '../icons';
import {memory, skill} from '../memory.styles';
import {useQueryRetry} from '../QueryBoundary';
import {badge, btn, iconBtn, page} from '../ui';
import type {McpFormState} from './mcpConfig';
import {formToConfigJson, prettyJson} from './mcpConfig';
import {McpServerModal} from './McpServerModal';
import {settings} from './settings.styles';

export function McpTab() {
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
  const totalTools = servers.reduce((n, s) => n + s.toolCount, 0);
  const boundTools = servers.reduce((n, s) => n + (s.loadMode === 'lazy' ? 0 : s.toolCount), 0);

  const refresh = refreshMcpServers;

  const addMut = useAsyncAction(
    async (form: McpFormState) => {
      const json = formToConfigJson(form);
      JSON.parse(json); // validate before sending
      const {commitAddMcpServer} = await import('../../relay/AddMcpServerMutation');
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
      const {commitUpdateMcpServer} = await import('../../relay/UpdateMcpServerMutation');
      await commitUpdateMcpServer(editing.name, json);
      toast.push('MCP server updated', 'success');
      setEditing(null);
      await refresh();
    },
    {onError: (e) => toast.push(e.message || String(e), 'error')},
  );

  const removeMut = useAsyncAction(
    async (name: string) => {
      const {commitRemoveMcpServer} = await import('../../relay/RemoveMcpServerMutation');
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
      const {commitReloadMcpServers} = await import('../../relay/ReloadMcpServersMutation');
      await commitReloadMcpServers();
      toast.push('MCP reloaded', 'success');
      await refresh();
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  const loadModeMut = useAsyncAction(
    async ({name, mode}: {name: string; mode: 'always' | 'lazy'}) => {
      const {commitSetMcpServerLoadMode} = await import('../../relay/SetMcpServerLoadModeMutation');
      await commitSetMcpServerLoadMode(name, mode);
      toast.push(
        mode === 'lazy'
          ? 'Tools unbound — the agent loads them on demand'
          : 'Tools bound to the agent',
        'success',
      );
      await refresh();
    },
    {onError: (e) => toast.push(e.message || String(e), 'error')},
  );

  return (
    <div {...stylex.props(page.section)}>
      <h2 {...stylex.props(page.sectionTitle)}>
        Servers <span {...stylex.props(page.count)}>{servers.length}</span>
        <span {...stylex.props(page.sectionHint)}>
          {totalTools} tools loaded · {boundTools} in every prompt
        </span>
        <span {...stylex.props(settings.sectionActions)}>
          <button
            {...stylex.props(btn.base)}
            onClick={() => void reloadMut.run()}
            disabled={reloadMut.pending}
            title="Reload MCP connections"
          >
            {reloadMut.pending ? 'Reloading…' : 'Reload'}
          </button>
          <button {...stylex.props(btn.base, btn.primary)} onClick={() => setShowAdd(true)}>
            <PlusIcon size={14} /> Add server
          </button>
        </span>
      </h2>

      {servers.length === 0 ? (
        <div {...stylex.props(page.empty)}>
          <p>No MCP servers configured.</p>
          <p>
            MCP servers extend the agent with external tools — start from a preset or add your own
            stdio/HTTP server.
          </p>
          <button {...stylex.props(btn.base, btn.primary)} onClick={() => setShowAdd(true)}>
            <PlusIcon size={14} /> Add your first server
          </button>
        </div>
      ) : (
        <ul {...stylex.props(page.list)}>
          {servers.map((s: any) => (
            <McpServerCard
              key={s.name}
              server={s}
              loadModePending={loadModeMut.pending}
              onToggleLoadMode={(name, mode) => void loadModeMut.run({name, mode})}
              onEdit={setEditing}
              onDelete={setDeleteTarget}
              onCopyConfig={(config) => {
                navigator.clipboard.writeText(config);
                toast.push('Config copied', 'success');
              }}
            />
          ))}
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
            Remove <strong>{deleteTarget}</strong>? This deletes the DB override — env/file sources
            remain.
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

function McpServerCard({
  server: s,
  loadModePending,
  onToggleLoadMode,
  onEdit,
  onDelete,
  onCopyConfig,
}: {
  server: any;
  loadModePending: boolean;
  onToggleLoadMode: (name: string, mode: 'always' | 'lazy') => void;
  onEdit: (target: {name: string; config: string}) => void;
  onDelete: (name: string) => void;
  onCopyConfig: (config: string) => void;
}) {
  const serverTools: string[] = s.tools ?? [];
  const lazy = s.loadMode === 'lazy';

  return (
    <li {...stylex.props(skill.card)}>
      <div {...stylex.props(skill.head)}>
        <span {...stylex.props(skill.name)}>{s.name}</span>
        <span
          {...stylex.props(
            badge.base,
            s.transport === 'stdio' ? badge.stdio : s.transport === 'http' ? badge.http : badge.sse,
          )}
        >
          {s.transport}
        </span>
        {s.toolCount > 0 ? (
          <span {...stylex.props(badge.base, badge.live)}>{s.toolCount} tools</span>
        ) : (
          <span {...stylex.props(badge.base)}>not loaded</span>
        )}
        <span
          {...stylex.props(badge.base)}
          title={
            lazy
              ? 'Tool schemas stay out of the prompt. The agent discovers and calls them on demand via jarvis.mcp_call.'
              : 'Tool schemas are sent to the model on every call of every run in this conversation.'
          }
        >
          {lazy ? 'on demand' : 'always loaded'}
        </span>
        <div {...stylex.props(skill.controls)}>
          <button
            {...stylex.props(btn.base, btn.small)}
            disabled={loadModePending}
            title={
              lazy ? 'Bind these tools to the agent' : 'Keep these tool schemas out of the prompt'
            }
            onClick={() => onToggleLoadMode(s.name, lazy ? 'always' : 'lazy')}
          >
            {lazy ? 'Always load' : 'Load on demand'}
          </button>
          <button
            {...stylex.props(iconBtn.base)}
            title="Edit server"
            onClick={() => onEdit({name: s.name, config: s.config})}
          >
            <EditIcon size={14} />
          </button>
          <button
            {...stylex.props(iconBtn.base, iconBtn.danger)}
            title="Remove server"
            onClick={() => onDelete(s.name)}
          >
            <TrashIcon size={14} />
          </button>
        </div>
      </div>
      <p {...stylex.props(skill.desc, settings.mono)}>{s.command || s.url || 'custom config'}</p>
      <details {...stylex.props(skill.body)}>
        <summary>Configuration{serverTools.length > 0 ? ' & tools' : ''}</summary>
        {serverTools.length > 0 && (
          <div {...stylex.props(settings.toolChips)}>
            {serverTools.map((t) => (
              <span key={t} {...stylex.props(badge.base)}>
                {t}
              </span>
            ))}
          </div>
        )}
        {s.toolCount === 0 && (
          <p {...stylex.props(memory.sectionEmpty)}>
            Server not loaded — tools appear after a successful connection. Try Reload after adding.
          </p>
        )}
        {lazy && s.toolCount > 0 && (
          <p {...stylex.props(memory.sectionEmpty)}>
            Loaded but not bound: these schemas cost nothing per LLM call. The agent sees the server
            name and tool list, and calls one with{' '}
            <code>
              jarvis.mcp_call(&quot;{s.name}&quot;, &quot;tool&quot;, {'{…}'})
            </code>
            .
          </p>
        )}
        <pre>{prettyJson(s.config)}</pre>
        <button {...stylex.props(btn.base, btn.small)} onClick={() => onCopyConfig(s.config)}>
          Copy JSON
        </button>
      </details>
    </li>
  );
}
