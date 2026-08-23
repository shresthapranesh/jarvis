import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ModelCatalogQuery as TModelCatalogQuery} from '../../__generated__/ModelCatalogQuery.graphql';
import {useAsyncAction} from '../../hooks/useAsyncAction';
import {useToast} from '../../lib/toast';
import type {CatalogModel, ModelCatalogData} from '../../relay/ModelCatalogQuery';
import {modelCatalogQuery, refreshModelCatalog} from '../../relay/ModelCatalogQuery';
import {ConfirmDialog} from '../ConfirmDialog';
import {FormModal} from '../FormModal';
import {CheckIcon, EditIcon, PlusIcon, SearchIcon, SyncIcon, TrashIcon} from '../icons';
import {useQueryRetry} from '../QueryBoundary';
import {ModelSyncModal} from './ModelSyncModal';

type ModelEditor = {mode: 'add'} | {mode: 'edit'; model: CatalogModel};

export function ModelsTab() {
  const toast = useToast();
  const queryData = useLazyLoadQuery<TModelCatalogQuery>(
    modelCatalogQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const data = queryData?.models as ModelCatalogData | undefined;

  const [filter, setFilter] = useState('');
  const [providerFilter, setProviderFilter] = useState<string>('all');
  const [editor, setEditor] = useState<ModelEditor | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<CatalogModel | null>(null);
  const [syncOpen, setSyncOpen] = useState(false);

  const refresh = refreshModelCatalog;

  const saveMut = useAsyncAction(
    async (draft: {id: string; label: string; provider: string}) => {
      const provider = draft.provider || null;
      const wasEdit = editor?.mode === 'edit';
      if (wasEdit) {
        const {commitUpdateModel} = await import('../../relay/UpdateModelMutation');
        await commitUpdateModel(draft.id, draft.label, provider);
      } else {
        const {commitAddModel} = await import('../../relay/AddModelMutation');
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
      const {commitRemoveModel} = await import('../../relay/RemoveModelMutation');
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
      const {commitSetDefaultModel} = await import('../../relay/SetDefaultModelMutation');
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
          <button
            className="artifact-btn"
            title="Diff the catalog against what each provider offers"
            onClick={() => setSyncOpen(true)}
          >
            <SyncIcon size={14} /> Sync
          </button>
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
                <p className="skill-card-desc">
                  {m.label}
                  {m.contextWindow ? (
                    <span className="settings-model-window">
                      {' · '}
                      {m.contextWindow.toLocaleString()} ctx
                    </span>
                  ) : null}
                </p>
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

      {syncOpen && (
        <ModelSyncModal
          providers={data?.discoverableProviders ?? []}
          onClose={() => setSyncOpen(false)}
          onCatalogChanged={refresh}
        />
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
            Remove <strong>{deleteTarget?.id}</strong> from the catalog? Conversations pinned to it
            fall back to the default model.
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
    editing && editor.model.provider !== editor.model.id.split(':')[0] ? editor.model.provider : '',
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
          <code>provider:model_name</code> — provider must be one of {providers.join(', ')}.
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
          Which backend builds the client. Override only when the ID prefix isn't the provider.
        </span>
      </div>
    </FormModal>
  );
}
