import {useEffect, useState} from 'react';

import {useModels} from '../hooks/useModels';
import type {
  Automation,
  AutomationInputType,
  CreateAutomationPayload,
  NotificationConfig,
} from '../lib/types';
import {
  NotificationsEditor,
  parseNotifications,
  serializeNotifications,
} from './NotificationsEditor';

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
    if ((form.input_type === 'prompt' || form.input_type === 'monitor') && !form.prompt_text?.trim()) {
      setError(form.input_type === 'monitor' ? 'Describe what to monitor' : 'Prompt text is required');
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
    <form className="auto-form" onSubmit={handleSubmit}>
      <div className="auto-form-group">
        <label className="auto-form-label">Name</label>
        <input
          className="auto-form-input"
          value={form.name}
          onChange={(e) => set('name', e.target.value)}
          placeholder="My automation"
          disabled={saving}
        />
      </div>

      <div className="auto-form-group">
        <label className="auto-form-label">Description (optional)</label>
        <input
          className="auto-form-input"
          value={form.description ?? ''}
          onChange={(e) => set('description', e.target.value)}
          placeholder="What does this do?"
          disabled={saving}
        />
      </div>

      <div className="auto-form-group">
        <label className="auto-form-label">Input Type</label>
        <select
          className="auto-form-select"
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
          <div className="auto-form-group">
            <label className="auto-form-label">
              {form.input_type === 'monitor' ? 'What to monitor' : 'Prompt'}
            </label>
            <textarea
              className="auto-form-textarea"
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
              <div className="auto-form-hint">
                Runs remember previous checks and only notify when something changed.
              </div>
            )}
          </div>
          <div className="auto-form-group">
            <label className="auto-form-label">Model</label>
            <select
              className="auto-form-select"
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
            <div className="auto-form-check-row">
              <input
                id="auto-stateful"
                type="checkbox"
                checked={form.stateful ?? false}
                onChange={(e) => set('stateful', e.target.checked)}
                disabled={saving}
              />
              <label htmlFor="auto-stateful" className="auto-form-check-label">
                Stateful — runs share one conversation, so the agent remembers previous runs
              </label>
            </div>
          )}
        </>
      )}

      {form.input_type === 'code' && (
        <div className="auto-form-group">
          <label className="auto-form-label">Python Code</label>
          <textarea
            className="auto-form-textarea auto-form-code"
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
          <div className="auto-form-group">
            <label className="auto-form-label">URL</label>
            <input
              className="auto-form-input"
              type="url"
              value={form.webhook_url ?? ''}
              onChange={(e) => set('webhook_url', e.target.value)}
              placeholder="https://your-n8n.example.com/webhook/..."
              disabled={saving}
            />
          </div>
          <div className="auto-form-group">
            <label className="auto-form-label">Method</label>
            <select
              className="auto-form-select"
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
          <div className="auto-form-group">
            <label className="auto-form-label">Headers (JSON)</label>
            <textarea
              className="auto-form-textarea auto-form-code"
              rows={3}
              value={form.webhook_headers ?? ''}
              onChange={(e) => set('webhook_headers', e.target.value)}
              placeholder={'{"Content-Type": "application/json"}'}
              disabled={saving}
              spellCheck={false}
            />
          </div>
          <div className="auto-form-group">
            <label className="auto-form-label">Body</label>
            <textarea
              className="auto-form-textarea auto-form-code"
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

      <div className="auto-form-group">
        <label className="auto-form-label">
          Schedule (cron in server local time, leave blank for ad-hoc)
        </label>
        <input
          className="auto-form-input"
          value={form.schedule ?? ''}
          onChange={(e) => set('schedule', e.target.value)}
          placeholder="0 9 * * 1  (Mon 9am)"
          disabled={saving}
        />
      </div>

      <NotificationsEditor
        value={notifications}
        onChange={setNotifications}
        disabled={saving}
      />

      <div className="auto-form-check-row">
        <input
          id="auto-enabled"
          type="checkbox"
          checked={form.enabled}
          onChange={(e) => set('enabled', e.target.checked)}
          disabled={saving}
        />
        <label htmlFor="auto-enabled" className="auto-form-check-label">
          Enabled
        </label>
      </div>

      {error && <div className="error-bubble">{error}</div>}

      <div className="auto-form-footer">
        <button
          type="button"
          className="activity-btn auto-form-cancel-btn"
          onClick={onCancel}
          disabled={saving}
        >
          Cancel
        </button>
        <button type="submit" className="auto-form-save-btn" disabled={saving}>
          {saving ? 'Saving…' : initialValues ? 'Update' : 'Create'}
        </button>
      </div>
    </form>
  );
}
