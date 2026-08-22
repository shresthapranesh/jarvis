import {useMemo, useState} from 'react';

import {FormModal} from '../FormModal';
import {PlusIcon, TrashIcon} from '../icons';
import type {McpFormState, McpPreset, McpTransport} from './mcpConfig';
import {MCP_PRESETS, configToForm, emptyForm, formToConfigJson, prettyJson} from './mcpConfig';

export function McpServerModal({
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
  const [form, setForm] = useState<McpFormState>(() =>
    initial ? configToForm(initial.name, initial.config) : emptyForm(),
  );

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

  function removeAt(key: 'args' | 'env' | 'headers', i: number) {
    setForm((f) => ({...f, [key]: (f[key] as any[]).filter((_, idx) => idx !== i)}));
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
              <button
                key={p.id}
                type="button"
                className="settings-preset"
                onClick={() => applyPreset(p)}
              >
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
                  onClick={() => removeAt('args', i)}
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
                  onClick={() => removeAt('env', i)}
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
                  onClick={() => removeAt('headers', i)}
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
