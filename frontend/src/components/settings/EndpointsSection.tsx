import * as stylex from '@stylexjs/stylex';
import {useMemo, useState} from 'react';

import {useAsyncAction} from '../../hooks/useAsyncAction';
import {useToast} from '../../lib/toast';
import type {ModelEndpoint} from '../../relay/ModelCatalogQuery';
import {ConfirmDialog} from '../ConfirmDialog';
import {FormModal} from '../FormModal';
import {EditIcon, PlusIcon, TrashIcon} from '../icons';
import {skill} from '../memory.styles';
import {badge, btn, field, iconBtn, page} from '../ui';
import {models, settings, sync} from './settings.styles';

/** Well-known OpenAI-compatible servers: a name and the URL their docs give. */
const PRESETS = [
  {name: 'openai', label: 'OpenAI', url: 'https://api.openai.com/v1', desc: 'api.openai.com'},
  {name: 'groq', label: 'Groq', url: 'https://api.groq.com/openai/v1', desc: 'api.groq.com'},
  {
    name: 'together',
    label: 'Together',
    url: 'https://api.together.xyz/v1',
    desc: 'api.together.xyz',
  },
  {
    name: 'deepseek',
    label: 'DeepSeek',
    url: 'https://api.deepseek.com/v1',
    desc: 'api.deepseek.com',
  },
  {name: 'mistral', label: 'Mistral', url: 'https://api.mistral.ai/v1', desc: 'api.mistral.ai'},
  {name: 'lmstudio', label: 'LM Studio', url: 'http://localhost:1234/v1', desc: 'local, port 1234'},
  {name: 'vllm', label: 'vLLM', url: 'http://localhost:8000/v1', desc: 'local, port 8000'},
  {name: 'llamacpp', label: 'llama.cpp', url: 'http://localhost:8080/v1', desc: 'local, port 8080'},
] as const;

// Mirrors core.model_catalog.ENDPOINT_NAME; the server has the final say.
const NAME = /^[a-z0-9][a-z0-9_-]{0,31}$/;

type Editor = {mode: 'add'} | {mode: 'edit'; endpoint: ModelEndpoint};

type Draft = {name: string; baseUrl: string; apiKey: string; clearKey: boolean};

/**
 * Settings → Models → Endpoints: OpenAI-compatible servers. Each one's name
 * becomes a provider, so its models are added as `name:model` — by hand, or
 * from Sync, which lists the server's `/models`.
 */
export function EndpointsSection({
  endpoints,
  providers,
  usedBy,
  onChanged,
}: {
  endpoints: readonly ModelEndpoint[];
  /** Every provider name taken, so a new endpoint can't shadow one. */
  providers: readonly string[];
  /** Model ids per endpoint name — an endpoint in use can't be removed. */
  usedBy: ReadonlyMap<string, readonly string[]>;
  onChanged: () => Promise<unknown>;
}) {
  const toast = useToast();
  const [editor, setEditor] = useState<Editor | null>(null);
  const [removing, setRemoving] = useState<ModelEndpoint | null>(null);

  const saveMut = useAsyncAction(
    async (d: Draft) => {
      const key = d.apiKey.trim() || null;
      if (editor?.mode === 'edit') {
        const {commitUpdateEndpoint} = await import('../../relay/UpdateEndpointMutation');
        await commitUpdateEndpoint(d.name, d.baseUrl.trim(), key, d.clearKey);
        toast.push('Endpoint updated', 'success');
      } else {
        const {commitAddEndpoint} = await import('../../relay/AddEndpointMutation');
        await commitAddEndpoint(d.name.trim(), d.baseUrl.trim(), key);
        toast.push(
          `Endpoint added — add its models as ${d.name.trim()}:<model>, or with Sync`,
          'success',
        );
      }
      setEditor(null);
      await onChanged();
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  const removeMut = useAsyncAction(
    async (name: string) => {
      const {commitRemoveEndpoint} = await import('../../relay/RemoveEndpointMutation');
      await commitRemoveEndpoint(name);
      toast.push('Endpoint removed', 'success');
      setRemoving(null);
      await onChanged();
    },
    {
      onError: (e) => {
        setRemoving(null);
        toast.push(e.message, 'error');
      },
    },
  );

  // An endpoint in use can't be removed, so there is nothing to confirm —
  // say why instead.
  const askRemove = (e: ModelEndpoint) => {
    const using = usedBy.get(e.name) ?? [];
    if (using.length > 0) {
      toast.push(`${e.name} is used by ${using.join(', ')} — remove those models first`, 'error');
      return;
    }
    setRemoving(e);
  };

  return (
    <div {...stylex.props(page.section)}>
      <h2 {...stylex.props(page.sectionTitle)}>
        Endpoints <span {...stylex.props(page.count)}>{endpoints.length}</span>
        <span {...stylex.props(page.sectionHint)}>OpenAI-compatible servers</span>
        <span {...stylex.props(settings.sectionActions)}>
          <button {...stylex.props(btn.base)} onClick={() => setEditor({mode: 'add'})}>
            <PlusIcon size={14} /> Add endpoint
          </button>
        </span>
      </h2>

      {endpoints.length === 0 ? (
        <div {...stylex.props(page.empty)}>
          None yet. Add any server that speaks OpenAI's API (OpenAI, Groq, vLLM, LM Studio,
          llama.cpp…) and its name becomes a provider for your models.
        </div>
      ) : (
        <ul {...stylex.props(models.grid)}>
          {endpoints.map((e) => {
            const count = usedBy.get(e.name)?.length ?? 0;
            return (
              <li key={e.name} {...stylex.props(skill.card, models.card)}>
                <div {...stylex.props(skill.head)}>
                  <span {...stylex.props(skill.name, models.id)}>{e.name}</span>
                  <div {...stylex.props(skill.controls)}>
                    <button
                      {...stylex.props(iconBtn.base)}
                      title="Edit endpoint"
                      onClick={() => setEditor({mode: 'edit', endpoint: e})}
                    >
                      <EditIcon size={14} />
                    </button>
                    <button
                      {...stylex.props(iconBtn.base, iconBtn.danger)}
                      title="Remove endpoint"
                      onClick={() => askRemove(e)}
                    >
                      <TrashIcon size={14} />
                    </button>
                  </div>
                </div>
                <span {...stylex.props(settings.mono, models.window)}>{e.baseUrl}</span>
                <div {...stylex.props(skill.head, skill.headStart)}>
                  <span {...stylex.props(badge.base, e.hasKey && badge.live)}>
                    {e.hasKey ? 'key set' : 'no key'}
                  </span>
                  <span {...stylex.props(badge.base)}>
                    {count} model{count === 1 ? '' : 's'}
                  </span>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      {editor && (
        <EndpointModal
          editor={editor}
          taken={providers}
          pending={saveMut.pending}
          onSubmit={(d) => void saveMut.run(d)}
          onClose={() => setEditor(null)}
        />
      )}

      <ConfirmDialog
        open={removing !== null}
        title="Remove endpoint"
        message={
          <p>
            Remove <strong>{removing?.name}</strong> and its stored key?
          </p>
        }
        confirmLabel="Remove"
        danger
        onConfirm={() => removing && void removeMut.run(removing.name)}
        onCancel={() => setRemoving(null)}
      />
    </div>
  );
}

function EndpointModal({
  editor,
  taken,
  pending,
  onSubmit,
  onClose,
}: {
  editor: Editor;
  taken: readonly string[];
  pending: boolean;
  onSubmit: (d: Draft) => void;
  onClose: () => void;
}) {
  const editing = editor.mode === 'edit';
  const hasKey = editing && editor.endpoint.hasKey;
  const [draft, setDraft] = useState<Draft>({
    name: editing ? editor.endpoint.name : '',
    baseUrl: editing ? editor.endpoint.baseUrl : '',
    apiKey: '',
    clearKey: false,
  });
  const set = <K extends keyof Draft>(k: K, v: Draft[K]) => setDraft((d) => ({...d, [k]: v}));

  const error = useMemo(() => {
    const name = draft.name.trim();
    if (!editing && name) {
      if (!NAME.test(name))
        return 'Name: 1-32 of a-z, 0-9, - or _, starting with a letter or digit';
      if (taken.includes(name)) return `'${name}' is already a provider`;
    }
    const url = draft.baseUrl.trim();
    if (url && !/^https?:\/\//.test(url)) return 'Base URL must start with http:// or https://';
    return null;
  }, [draft, editing, taken]);

  const canSubmit = Boolean(draft.name.trim() && draft.baseUrl.trim()) && error === null;

  return (
    <FormModal
      open
      title={editing ? `Edit ${editor.endpoint.name}` : 'Add endpoint'}
      subtitle={
        editing
          ? 'The name is part of every model id under it, so it cannot change.'
          : "Any server that speaks OpenAI's Chat Completions API. Its name becomes the provider prefix of its models."
      }
      submitLabel={editing ? 'Save changes' : 'Add endpoint'}
      submitDisabled={!canSubmit}
      pending={pending}
      error={error}
      onSubmit={() => canSubmit && onSubmit(draft)}
      onClose={onClose}
    >
      {!editing && (
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Quick presets</span>
          <div {...stylex.props(settings.presetStrip)}>
            {PRESETS.filter((p) => !taken.includes(p.name)).map((p) => (
              <button
                key={p.name}
                type="button"
                {...stylex.props(settings.preset)}
                onClick={() => setDraft((d) => ({...d, name: p.name, baseUrl: p.url}))}
              >
                <span {...stylex.props(settings.presetName)}>{p.label}</span>
                <span {...stylex.props(settings.presetDesc)}>{p.desc}</span>
              </button>
            ))}
          </div>
        </div>
      )}
      <div {...stylex.props(field.group)}>
        <span {...stylex.props(field.label)}>Name</span>
        <input
          {...stylex.props(field.input, settings.mono)}
          placeholder="groq"
          value={draft.name}
          onChange={(e) => set('name', e.target.value)}
          disabled={editing}
          spellCheck={false}
          autoFocus={!editing}
        />
        <span {...stylex.props(field.hint)}>
          Models under it are <code>{draft.name.trim() || 'name'}:model</code>.
        </span>
      </div>
      <div {...stylex.props(field.group)}>
        <span {...stylex.props(field.label)}>Base URL</span>
        <input
          {...stylex.props(field.input, settings.mono)}
          placeholder="https://api.groq.com/openai/v1"
          value={draft.baseUrl}
          onChange={(e) => set('baseUrl', e.target.value)}
          spellCheck={false}
          autoFocus={editing}
        />
        <span {...stylex.props(field.hint)}>
          The part before <code>/chat/completions</code> — usually ends in <code>/v1</code>.
        </span>
      </div>
      <div {...stylex.props(field.group)}>
        <span {...stylex.props(field.label)}>API key</span>
        <input
          {...stylex.props(field.input, settings.mono)}
          type="password"
          autoComplete="off"
          placeholder={
            hasKey ? '•••• stored — type to replace' : 'none (a local server may not need one)'
          }
          value={draft.apiKey}
          onChange={(e) => set('apiKey', e.target.value)}
          disabled={draft.clearKey}
          spellCheck={false}
        />
        {hasKey ? (
          <label {...stylex.props(sync.check)}>
            <input
              type="checkbox"
              checked={draft.clearKey}
              onChange={(e) => set('clearKey', e.target.checked)}
            />
            Remove the stored key
          </label>
        ) : (
          <span {...stylex.props(field.hint)}>Stored on the server and never shown again.</span>
        )}
      </div>
    </FormModal>
  );
}
