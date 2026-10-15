import * as stylex from '@stylexjs/stylex';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ModelCatalogQuery as TModelCatalogQuery} from '../../__generated__/ModelCatalogQuery.graphql';
import {useAsyncAction} from '../../hooks/useAsyncAction';
import {EFFORT_LEVELS, effortLabel} from '../../lib/effort';
import {useToast} from '../../lib/toast';
import type {CatalogModel, ModelCatalogData} from '../../relay/ModelCatalogQuery';
import {modelCatalogQuery, refreshModelCatalog} from '../../relay/ModelCatalogQuery';
import {colors, type} from '../../theme/tokens.stylex';
import {ConfirmDialog} from '../ConfirmDialog';
import {FormModal} from '../FormModal';
import {EditIcon, PlusIcon, SearchIcon, SyncIcon, TrashIcon} from '../icons';
import {skill} from '../memory.styles';
import {useQueryRetry} from '../QueryBoundary';
import {badge, btn, field, iconBtn, page} from '../ui';
import {EndpointsSection} from './EndpointsSection';
import {ModelSyncModal} from './ModelSyncModal';
import {models, settings} from './settings.styles';

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

  const usedBy = useMemo(() => {
    const out = new Map<string, string[]>();
    for (const m of data?.available ?? [])
      out.set(m.provider, [...(out.get(m.provider) ?? []), m.id]);
    return out;
  }, [data]);

  return (
    <>
      {data && (
        <EffortSection
          levels={data.effortLevels}
          defaultEffort={data.defaultEffort}
          onChanged={refresh}
        />
      )}
      <EndpointsSection
        endpoints={data?.endpoints ?? []}
        providers={data?.providers ?? []}
        usedBy={usedBy}
        onChanged={refresh}
      />
      <div {...stylex.props(page.section)}>
        <h2 {...stylex.props(page.sectionTitle)}>
          Model catalog <span {...stylex.props(page.count)}>{data?.available?.length ?? 0}</span>
          <span {...stylex.props(page.sectionHint)}>{customCount} custom</span>
          <span {...stylex.props(settings.sectionActions)}>
            <button
              {...stylex.props(btn.base)}
              title="Diff the catalog against what each provider offers"
              onClick={() => setSyncOpen(true)}
            >
              <SyncIcon size={14} /> Sync
            </button>
            <button
              {...stylex.props(btn.base, btn.primary)}
              onClick={() => setEditor({mode: 'add'})}
            >
              <PlusIcon size={14} /> Add model
            </button>
          </span>
        </h2>

        <div {...stylex.props(settings.filterRow)}>
          <div {...stylex.props(settings.search)}>
            <SearchIcon size={14} />
            <input
              {...stylex.props(settings.searchInput)}
              placeholder="Search models…"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
            />
          </div>
          <select
            {...stylex.props(field.select, settings.filterSelect, field.selectChrome)}
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
          {/* Exactly one model can be the default, so this is one control — not
            a "Set as default" button repeated on every card in the grid. It
            lists the whole catalog, not just the current filter, so narrowing
            the grid can never hide the model you are trying to select. */}
          <label {...stylex.props(models.defaultPicker)}>
            <span {...stylex.props(page.sectionHint)}>Default</span>
            <select
              {...stylex.props(field.select, models.defaultSelect, field.selectChrome)}
              value={data?.default ?? ''}
              disabled={defaultMut.pending}
              onChange={(e) => void defaultMut.run(e.target.value)}
              title="The model used when a run does not name one"
            >
              {!data?.default && <option value="">—</option>}
              {(data?.available ?? []).map((m) => (
                <option key={m.id} value={m.id}>
                  {m.id}
                </option>
              ))}
            </select>
          </label>
        </div>

        {filtered.length === 0 ? (
          <div {...stylex.props(page.empty)}>No models match the filter.</div>
        ) : (
          <ul {...stylex.props(models.grid)}>
            {filtered.map((m) => {
              const isDefault = m.id === data?.default;
              return (
                <li key={m.id} {...stylex.props(skill.card, models.card)}>
                  <div {...stylex.props(skill.head)}>
                    <span {...stylex.props(skill.name, models.name)}>{m.label || m.id}</span>
                    {/* The provider is the id's prefix almost always; tag it only
                      when it isn't, so the card doesn't say it twice. */}
                    {m.provider !== m.id.split(':')[0] && (
                      <span {...stylex.props(badge.base)}>{m.provider}</span>
                    )}
                    {isDefault && <span {...stylex.props(badge.base, badge.live)}>default</span>}
                    {!m.builtin && <span {...stylex.props(badge.base)}>custom</span>}
                    {!m.builtin && (
                      <div {...stylex.props(skill.controls)}>
                        <button
                          {...stylex.props(iconBtn.base)}
                          title="Edit model"
                          onClick={() => setEditor({mode: 'edit', model: m})}
                        >
                          <EditIcon size={14} />
                        </button>
                        <button
                          {...stylex.props(iconBtn.base, iconBtn.danger)}
                          title="Remove model"
                          onClick={() => setDeleteTarget(m)}
                        >
                          <TrashIcon size={14} />
                        </button>
                      </div>
                    )}
                  </div>
                  <p {...stylex.props(skill.desc)}>
                    <span {...stylex.props(models.idLine)}>{m.id}</span>
                    {m.contextWindow ? (
                      <span {...stylex.props(models.window)}>
                        {' · '}
                        {m.contextWindow.toLocaleString()} ctx
                      </span>
                    ) : null}
                  </p>
                  {m.efforts.length > 0 && (
                    <span {...stylex.props(models.window)}>
                      Effort {m.efforts.map(effortLabel).join(' · ')}
                    </span>
                  )}
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
              Remove <strong>{deleteTarget?.id}</strong> from the catalog? Conversations pinned to
              it fall back to the default model.
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
    </>
  );
}

/**
 * Which effort levels the composer's picker offers, and the one a chat
 * starts at. Each model shows only the offered levels it takes, so turning
 * one off here takes it out of every model's row at once.
 */
function EffortSection({
  levels,
  defaultEffort,
  onChanged,
}: {
  levels: readonly string[];
  defaultEffort: string | null;
  onChanged: () => Promise<unknown>;
}) {
  const toast = useToast();
  const save = useAsyncAction(
    async (next: {levels: string[]; defaultEffort: string | null}) => {
      const {commitSetEfforts} = await import('../../relay/SetEffortsMutation');
      await commitSetEfforts(next.levels, next.defaultEffort);
      await onChanged();
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  function toggle(level: string) {
    const on = levels.includes(level);
    if (on && levels.length === 1) {
      toast.push('Offer at least one effort level', 'error');
      return;
    }
    const next = EFFORT_LEVELS.filter((l) => (l === level ? !on : levels.includes(l)));
    // A default that is no longer offered goes back to automatic.
    const nextDefault =
      defaultEffort && next.includes(defaultEffort as never) ? defaultEffort : null;
    void save.run({levels: [...next], defaultEffort: nextDefault});
  }

  return (
    <div {...stylex.props(page.section)}>
      <h2 {...stylex.props(page.sectionTitle)}>
        Effort
        <span {...stylex.props(page.sectionHint)}>
          How hard a model thinks — for the models that take it
        </span>
      </h2>
      <div {...stylex.props(effortStyles.row)}>
        <span {...stylex.props(effortStyles.label)}>Offered</span>
        <div {...stylex.props(effortStyles.chips)} role="group" aria-label="Offered effort levels">
          {EFFORT_LEVELS.map((level) => {
            const on = levels.includes(level);
            return (
              <button
                key={level}
                type="button"
                aria-pressed={on}
                disabled={save.pending}
                {...stylex.props(effortStyles.chip, on && effortStyles.chipOn)}
                onClick={() => toggle(level)}
              >
                {effortLabel(level)}
              </button>
            );
          })}
        </div>
      </div>
      <label {...stylex.props(effortStyles.row)}>
        <span {...stylex.props(effortStyles.label)}>Default</span>
        <select
          {...stylex.props(field.select, effortStyles.select, field.selectChrome)}
          value={defaultEffort ?? ''}
          disabled={save.pending}
          onChange={(e) =>
            void save.run({levels: [...levels], defaultEffort: e.target.value || null})
          }
        >
          <option value="">Automatic</option>
          {levels.map((level) => (
            <option key={level} value={level}>
              {effortLabel(level)}
            </option>
          ))}
        </select>
      </label>
      <span {...stylex.props(field.hint)}>
        The default applies to every model and every run; a pick in the composer overrides it for
        that conversation. Automatic runs chats at medium and unattended work (automations, board
        tasks, workflows) at high. A model that can&rsquo;t go as high runs at its highest level
        below.
      </span>
    </div>
  );
}

const effortStyles = stylex.create({
  row: {display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap', minWidth: 0},
  label: {width: 64, flexShrink: 0, fontSize: type.tUi, color: colors.textDim},
  chips: {display: 'flex', flexWrap: 'wrap', gap: 6},
  chip: {
    paddingBlock: 4,
    paddingInline: 10,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover:not(:disabled)': colors.borderStrong},
    borderRadius: 2,
    backgroundColor: 'transparent',
    color: {default: colors.textDim, ':hover:not(:disabled)': colors.text},
    fontSize: type.tSmall,
    fontFamily: 'inherit',
    cursor: {default: 'pointer', ':disabled': 'progress'},
  },
  chipOn: {
    backgroundColor: colors.accent,
    borderColor: {default: colors.accent, ':hover:not(:disabled)': colors.accent},
    color: {default: colors.accentContrast, ':hover:not(:disabled)': colors.accentContrast},
  },
  select: {flexGrow: 0, flexShrink: 1, flexBasis: 220, minWidth: 0},
});

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
      <div {...stylex.props(field.group)}>
        <span {...stylex.props(field.label)}>Model ID</span>
        <input
          {...stylex.props(field.input, settings.mono)}
          placeholder="google_genai:gemini-3.5-flash"
          value={id}
          onChange={(e) => setId(e.target.value)}
          disabled={editing}
          spellCheck={false}
          autoFocus={!editing}
        />
        <span {...stylex.props(field.hint)}>
          <code>provider:model_name</code> — provider must be one of {providers.join(', ')}.
        </span>
      </div>
      <div {...stylex.props(field.group)}>
        <span {...stylex.props(field.label)}>Label</span>
        <input
          {...stylex.props(field.input)}
          placeholder="Gemini 3.5 Flash (Google)"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          autoFocus={editing}
        />
        <span {...stylex.props(field.hint)}>Shown in the model dropdowns.</span>
      </div>
      <div {...stylex.props(field.group)}>
        <span {...stylex.props(field.label)}>Provider</span>
        <select
          {...stylex.props(field.select, field.selectChrome)}
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
        <span {...stylex.props(field.hint)}>
          Which backend builds the client. Override only when the ID prefix isn't the provider.
        </span>
      </div>
    </FormModal>
  );
}
