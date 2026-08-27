import * as stylex from '@stylexjs/stylex';
import {useEffect, useState} from 'react';

import {useModels} from '../hooks/useModels';
import type {
  Automation,
  AutomationInputType,
  CreateAutomationPayload,
  NotificationConfig,
} from '../lib/types';
import {channels, colors, type} from '../theme/tokens.stylex';
import {
  NotificationsEditor,
  parseNotifications,
  serializeNotifications,
} from './NotificationsEditor';
import {btn, errorBubble, field} from './ui';

interface Props {
  initialValues?: Automation;
  onSave: (payload: CreateAutomationPayload) => Promise<void>;
  onCancel: () => void;
}

const BLANK: CreateAutomationPayload = {
  name: '',
  description: '',
  input_type: 'prompt',
  prompt_text: '',
  // null means "fall back to backend DEFAULT_MODEL at run time"; the form
  // populates it from the catalog as soon as useModels resolves.
  model: null,
  code_text: '',
  webhook_url: '',
  webhook_method: 'POST',
  webhook_headers: '',
  webhook_body: '',
  schedule: '',
  enabled: true,
  stateful: false,
};

export function AutomationForm({initialValues, onSave, onCancel}: Props) {
  const {data: catalog} = useModels();
  const [form, setForm] = useState<CreateAutomationPayload>(
    initialValues
      ? {
          name: initialValues.name,
          description: initialValues.description ?? '',
          input_type: initialValues.input_type,
          prompt_text: initialValues.prompt_text ?? '',
          model: initialValues.model ?? null,
          code_text: initialValues.code_text ?? '',
          webhook_url: initialValues.webhook_url ?? '',
          webhook_method: initialValues.webhook_method ?? 'POST',
          webhook_headers: initialValues.webhook_headers ?? '',
          webhook_body: initialValues.webhook_body ?? '',
          schedule: initialValues.schedule ?? '',
          enabled: initialValues.enabled,
          stateful: initialValues.stateful,
        }
      : BLANK,
  );
  const [notifications, setNotifications] = useState<NotificationConfig[]>(
    parseNotifications(initialValues?.notifications),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Default the model once the catalog arrives (only if the form hasn't
  // been populated from an existing automation with its own model).
  useEffect(() => {
    if (catalog && !form.model) {
      setForm((f) => ({...f, model: catalog.default}));
    }
  }, [catalog, form.model]);

  function set(key: keyof CreateAutomationPayload, value: unknown) {
    setForm((f) => ({...f, [key]: value}));
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);

    if (!form.name.trim()) {
      setError('Name is required');
      return;
    }
    if (
      (form.input_type === 'prompt' || form.input_type === 'monitor') &&
      !form.prompt_text?.trim()
    ) {
      setError(
        form.input_type === 'monitor' ? 'Describe what to monitor' : 'Prompt text is required',
      );
      return;
    }
    if (form.input_type === 'code' && !form.code_text?.trim()) {
      setError('Code is required');
      return;
    }
    if (form.input_type === 'webhook' && !form.webhook_url?.trim()) {
      setError('Webhook URL is required');
      return;
    }

    const payload: CreateAutomationPayload = {
      ...form,
      description: form.description || null,
      schedule: form.schedule || null,
      prompt_text: form.prompt_text || null,
      model: form.model || null,
      code_text: form.code_text || null,
      webhook_url: form.webhook_url || null,
      webhook_method: form.webhook_method || null,
      webhook_headers: form.webhook_headers || null,
      webhook_body: form.webhook_body || null,
      notifications: serializeNotifications(notifications),
    };

    setSaving(true);
    try {
      await onSave(payload);
    } catch (err) {
      setError((err as Error).message);
      setSaving(false);
    }
  }

  return (
    <form {...stylex.props(styles.form)} onSubmit={handleSubmit}>
      <div {...stylex.props(field.group)}>
        <label {...stylex.props(field.label)}>Name</label>
        <input
          {...stylex.props(field.input)}
          value={form.name}
          onChange={(e) => set('name', e.target.value)}
          placeholder="My automation"
          disabled={saving}
        />
      </div>

      <div {...stylex.props(field.group)}>
        <label {...stylex.props(field.label)}>Description (optional)</label>
        <input
          {...stylex.props(field.input)}
          value={form.description ?? ''}
          onChange={(e) => set('description', e.target.value)}
          placeholder="What does this do?"
          disabled={saving}
        />
      </div>

      <div {...stylex.props(field.group)}>
        <label {...stylex.props(field.label)}>Input Type</label>
        <select
          {...stylex.props(field.select)}
          value={form.input_type}
          onChange={(e) => set('input_type', e.target.value as AutomationInputType)}
          disabled={saving}
        >
          <option value="prompt">Prompt</option>
          <option value="monitor">Monitor</option>
          <option value="code">Code</option>
          <option value="webhook">Webhook</option>
        </select>
      </div>

      {(form.input_type === 'prompt' || form.input_type === 'monitor') && (
        <>
          <div {...stylex.props(field.group)}>
            <label {...stylex.props(field.label)}>
              {form.input_type === 'monitor' ? 'What to monitor' : 'Prompt'}
            </label>
            <textarea
              {...stylex.props(field.textarea)}
              rows={5}
              value={form.prompt_text ?? ''}
              onChange={(e) => set('prompt_text', e.target.value)}
              placeholder={
                form.input_type === 'monitor'
                  ? 'The latest stable release of X; alert me when a new version ships...'
                  : 'Research the latest news about...'
              }
              disabled={saving}
            />
            {form.input_type === 'monitor' && (
              <div {...stylex.props(field.hint)}>
                Runs remember previous checks and only notify when something changed.
              </div>
            )}
          </div>
          <div {...stylex.props(field.group)}>
            <label {...stylex.props(field.label)}>Model</label>
            <select
              {...stylex.props(field.select)}
              value={form.model ?? ''}
              onChange={(e) => set('model', e.target.value)}
              disabled={saving || !catalog}
              title={catalog ? undefined : 'Loading models…'}
            >
              {!catalog && <option value="">Loading…</option>}
              {catalog?.available.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
            </select>
          </div>
          {form.input_type === 'prompt' && (
            <div {...stylex.props(styles.checkRow)}>
              <input
                id="auto-stateful"
                type="checkbox"
                checked={form.stateful ?? false}
                onChange={(e) => set('stateful', e.target.checked)}
                disabled={saving}
              />
              <label htmlFor="auto-stateful" {...stylex.props(styles.checkLabel)}>
                Stateful — runs share one conversation, so the agent remembers previous runs
              </label>
            </div>
          )}
        </>
      )}

      {form.input_type === 'code' && (
        <div {...stylex.props(field.group)}>
          <label {...stylex.props(field.label)}>Python Code</label>
          <textarea
            {...stylex.props(field.textarea, styles.code)}
            rows={10}
            value={form.code_text ?? ''}
            onChange={(e) => set('code_text', e.target.value)}
            placeholder="import datetime&#10;print(datetime.datetime.now())"
            disabled={saving}
            spellCheck={false}
          />
        </div>
      )}

      {form.input_type === 'webhook' && (
        <>
          <div {...stylex.props(field.group)}>
            <label {...stylex.props(field.label)}>URL</label>
            <input
              {...stylex.props(field.input)}
              type="url"
              value={form.webhook_url ?? ''}
              onChange={(e) => set('webhook_url', e.target.value)}
              placeholder="https://your-n8n.example.com/webhook/..."
              disabled={saving}
            />
          </div>
          <div {...stylex.props(field.group)}>
            <label {...stylex.props(field.label)}>Method</label>
            <select
              {...stylex.props(field.select)}
              value={form.webhook_method ?? 'POST'}
              onChange={(e) => set('webhook_method', e.target.value)}
              disabled={saving}
            >
              <option value="POST">POST</option>
              <option value="GET">GET</option>
              <option value="PUT">PUT</option>
              <option value="DELETE">DELETE</option>
            </select>
          </div>
          <div {...stylex.props(field.group)}>
            <label {...stylex.props(field.label)}>Headers (JSON)</label>
            <textarea
              {...stylex.props(field.textarea, styles.code)}
              rows={3}
              value={form.webhook_headers ?? ''}
              onChange={(e) => set('webhook_headers', e.target.value)}
              placeholder={'{"Content-Type": "application/json"}'}
              disabled={saving}
              spellCheck={false}
            />
          </div>
          <div {...stylex.props(field.group)}>
            <label {...stylex.props(field.label)}>Body</label>
            <textarea
              {...stylex.props(field.textarea, styles.code)}
              rows={4}
              value={form.webhook_body ?? ''}
              onChange={(e) => set('webhook_body', e.target.value)}
              placeholder={'{"key": "value"}'}
              disabled={saving}
              spellCheck={false}
            />
          </div>
        </>
      )}

      <div {...stylex.props(field.group)}>
        <label {...stylex.props(field.label)}>
          Schedule (cron in server local time, leave blank for ad-hoc)
        </label>
        <input
          {...stylex.props(field.input)}
          value={form.schedule ?? ''}
          onChange={(e) => set('schedule', e.target.value)}
          placeholder="0 9 * * 1  (Mon 9am)"
          disabled={saving}
        />
      </div>

      <NotificationsEditor value={notifications} onChange={setNotifications} disabled={saving} />

      <div {...stylex.props(styles.checkRow)}>
        <input
          id="auto-enabled"
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => set('enabled', e.target.checked)}
          disabled={saving}
        />
        <label htmlFor="auto-enabled" {...stylex.props(styles.checkLabel)}>
          Enabled
        </label>
      </div>

      {error && <div {...stylex.props(errorBubble.base)}>{error}</div>}

      <div {...stylex.props(styles.footer)}>
        <button type="button" {...stylex.props(btn.base)} onClick={onCancel} disabled={saving}>
          Cancel
        </button>
        <button type="submit" {...stylex.props(styles.saveBtn)} disabled={saving}>
          {saving ? 'Saving…' : initialValues ? 'Update' : 'Create'}
        </button>
      </div>
    </form>
  );
}

const styles = stylex.create({
  form: {display: 'flex', flexDirection: 'column', gap: 14, padding: 16},

  /** Monospace treatment for the code / JSON textareas. */
  code: {fontFamily: type.mono, fontSize: '0.78rem'},

  checkRow: {display: 'flex', alignItems: 'center', gap: 8},
  checkLabel: {
    fontSize: '0.82rem',
    color: colors.text,
    cursor: 'pointer',
    userSelect: 'none',
  },

  footer: {display: 'flex', justifyContent: 'flex-end', gap: 8, paddingBlockStart: 4},
  saveBtn: {
    backgroundImage: `linear-gradient(135deg, ${colors.accentStrong}, ${colors.accent})`,
    borderStyle: 'none',
    borderRadius: 8,
    color: colors.accentContrast,
    fontSize: '0.82rem',
    fontFamily: 'inherit',
    paddingBlock: 6,
    paddingInline: 18,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.4},
    boxShadow: {
      default: `0 2px 10px rgba(${channels.accent}, 0.3)`,
      ':hover:not(:disabled)': `0 4px 16px rgba(${channels.accent}, 0.45)`,
    },
    transform: {default: null, ':hover:not(:disabled)': 'translateY(-1px)'},
    transition: 'box-shadow 0.2s, transform 0.15s',
  },
});
