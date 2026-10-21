import * as stylex from '@stylexjs/stylex';
import {useEffect, useMemo, useRef, useState} from 'react';

import {useModels} from '../hooks/useModels';
import type {
  Automation,
  AutomationInputType,
  CreateAutomationPayload,
  NotificationConfig,
} from '../lib/types';
import {typeIcon} from '../routes/automation.styles';
import {sheet, typeCard, ed} from './AutomationEditor.styles';
import {TypeIcon} from './AutomationParts';
import {ConfirmDialog} from './ConfirmDialog';
import {XIcon} from './icons';
import {
  NotificationsEditor,
  parseNotifications,
  serializeNotifications,
} from './NotificationsEditor';
import {ScheduleEditor} from './ScheduleEditor';
import {btn, closeBtn, field, ResizeHandle, Switch, useResizableWidth} from './ui';

/* ── The create/edit sheet ─────────────────────────────────────────────
   A wide, resizable sheet in two columns: what the automation does on the
   left (the prompt, code or request — the part you actually write), and
   when it runs, which model, and who hears about it in a rail on the
   right. Below ~700px of sheet the rail drops under the work. The header
   and the Save bar stay put while the body scrolls. */

interface Props {
  /** null means create. */
  editing: Automation | null;
  /** Rejects with the message to show; the sheet stays open on failure. */
  onSave: (payload: CreateAutomationPayload) => Promise<void>;
  onClose: () => void;
}

type FieldKey = 'name' | 'prompt' | 'code' | 'url';

const TYPES: {id: AutomationInputType; label: string; blurb: string}[] = [
  {id: 'prompt', label: 'Prompt', blurb: 'The agent works a task'},
  {id: 'monitor', label: 'Monitor', blurb: 'Tells you what changed'},
  {id: 'code', label: 'Code', blurb: 'Runs a Python cell'},
  {id: 'webhook', label: 'Webhook', blurb: 'Calls a URL'},
];

const BLANK: CreateAutomationPayload = {
  name: '',
  description: '',
  input_type: 'prompt',
  prompt_text: '',
  // null until saved: the picker shows the catalog default, and a save
  // with nothing picked stores that default.
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

function initialForm(a: Automation | null): CreateAutomationPayload {
  if (!a) return BLANK;
  return {
    name: a.name,
    description: a.description ?? '',
    input_type: a.input_type,
    prompt_text: a.prompt_text ?? '',
    model: a.model ?? null,
    code_text: a.code_text ?? '',
    webhook_url: a.webhook_url ?? '',
    webhook_method: a.webhook_method ?? 'POST',
    webhook_headers: a.webhook_headers ?? '',
    webhook_body: a.webhook_body ?? '',
    schedule: a.schedule ?? '',
    enabled: a.enabled,
    stateful: a.stateful,
  };
}

/** The sheet's frame while the model catalog loads. */
export function AutomationEditorLoading({onClose}: {onClose: () => void}) {
  const [width] = useResizableWidth('jarvis.automationEditorWidth', 960);
  return (
    <>
      <div {...stylex.props(sheet.backdrop)} onClick={onClose} />
      <div
        {...stylex.props(sheet.root)}
        style={{'--sheet-w': `${width}px`} as React.CSSProperties}
        aria-busy="true"
      >
        <div {...stylex.props(sheet.loading)}>Loading…</div>
      </div>
    </>
  );
}

export function AutomationEditor({editing, onSave, onClose}: Props) {
  const {data: catalog} = useModels();
  const [width, setWidth] = useResizableWidth('jarvis.automationEditorWidth', 960);
  const [initial] = useState(() => ({
    form: initialForm(editing),
    notifications: parseNotifications(editing?.notifications),
  }));
  const [form, setForm] = useState(initial.form);
  const [notifications, setNotifications] = useState<NotificationConfig[]>(initial.notifications);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<{field?: FieldKey; message: string} | null>(null);
  const [confirmDiscard, setConfirmDiscard] = useState(false);
  const formRef = useRef<HTMLFormElement>(null);
  const nameRef = useRef<HTMLInputElement>(null);

  const dirty = useMemo(
    () => JSON.stringify({form, notifications}) !== JSON.stringify(initial),
    [form, notifications, initial],
  );

  function set<K extends keyof CreateAutomationPayload>(key: K, value: CreateAutomationPayload[K]) {
    setForm((f) => ({...f, [key]: value}));
    if (error) setError(null);
  }

  function requestClose() {
    if (saving) return;
    if (dirty) setConfirmDiscard(true);
    else onClose();
  }

  // Escape closes — through the discard check, and not while that dialog is
  // up, since its own Escape handler is answering the same keypress.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape' || confirmDiscard) return;
      requestClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  });

  useEffect(() => {
    if (!editing) nameRef.current?.focus();
  }, [editing]);

  function validate(): {field: FieldKey; message: string} | null {
    if (!form.name.trim()) return {field: 'name', message: 'Give it a name'};
    const t = form.input_type;
    if ((t === 'prompt' || t === 'monitor') && !form.prompt_text?.trim()) {
      return {
        field: 'prompt',
        message: t === 'monitor' ? 'Describe what to monitor' : 'Write the prompt',
      };
    }
    if (t === 'code' && !form.code_text?.trim()) return {field: 'code', message: 'Add the code'};
    if (t === 'webhook' && !form.webhook_url?.trim()) {
      return {field: 'url', message: 'Add the webhook URL'};
    }
    return null;
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (saving) return;
    const invalid = validate();
    if (invalid) {
      setError(invalid);
      document.getElementById(`auto-${invalid.field}`)?.focus();
      return;
    }
    setError(null);

    const payload: CreateAutomationPayload = {
      ...form,
      name: form.name.trim(),
      description: form.description?.trim() || null,
      schedule: form.schedule?.trim() || null,
      prompt_text: form.prompt_text || null,
      model: form.model || catalog?.default || null,
      code_text: form.code_text || null,
      webhook_url: form.webhook_url?.trim() || null,
      webhook_method: form.webhook_method || null,
      webhook_headers: form.webhook_headers || null,
      webhook_body: form.webhook_body || null,
      notifications: serializeNotifications(notifications),
    };

    setSaving(true);
    try {
      await onSave(payload);
    } catch (err) {
      setError({message: (err as Error).message || 'Failed to save'});
      setSaving(false);
    }
  }

  function onFormKeyDown(e: React.KeyboardEvent) {
    if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
      e.preventDefault();
      formRef.current?.requestSubmit();
    }
  }

  const t = form.input_type;
  const agentic = t === 'prompt' || t === 'monitor';
  const bad = (f: FieldKey) => error?.field === f && ed.invalid;
  const mac = typeof navigator !== 'undefined' && /Mac|iPhone|iPad/.test(navigator.platform);

  return (
    <>
      <div {...stylex.props(sheet.backdrop)} onClick={requestClose} />
      <form
        ref={formRef}
        {...stylex.props(sheet.root)}
        style={{'--sheet-w': `${width}px`} as React.CSSProperties}
        onSubmit={handleSubmit}
        onKeyDown={onFormKeyDown}
        role="dialog"
        aria-modal="true"
        aria-label={editing ? `Edit ${editing.name}` : 'New automation'}
        noValidate
      >
        <ResizeHandle width={width} onResize={setWidth} label="Resize automation editor" />

        <header {...stylex.props(sheet.header)}>
          <div {...stylex.props(typeIcon.base, typeIcon[t], sheet.headerIcon)}>
            <TypeIcon type={t} size={18} />
          </div>
          <div {...stylex.props(sheet.titleBlock)}>
            <span {...stylex.props(sheet.eyebrow)}>
              {editing ? 'Edit automation' : 'New automation'}
            </span>
            <input
              id="auto-name"
              ref={nameRef}
              {...stylex.props(sheet.nameInput, bad('name'))}
              value={form.name}
              onChange={(e) => set('name', e.target.value)}
              placeholder="Name this automation"
              aria-label="Name"
              aria-invalid={error?.field === 'name'}
              disabled={saving}
              autoComplete="off"
            />
            <input
              {...stylex.props(sheet.descInput)}
              value={form.description ?? ''}
              onChange={(e) => set('description', e.target.value)}
              placeholder="Add a short description (optional)"
              aria-label="Description"
              disabled={saving}
              autoComplete="off"
            />
          </div>
          <div {...stylex.props(sheet.headerActions)}>
            <Switch
              checked={form.enabled}
              onChange={(v) => set('enabled', v)}
              disabled={saving}
              label={form.enabled ? 'Enabled' : 'Paused'}
              title={form.enabled ? 'Runs on its schedule' : 'The schedule is paused'}
            />
            <button
              type="button"
              {...stylex.props(closeBtn.base)}
              onClick={requestClose}
              aria-label="Close"
            >
              <XIcon size={14} />
            </button>
          </div>
        </header>

        <div {...stylex.props(sheet.body)}>
          <div {...stylex.props(sheet.grid)}>
            {/* ── What it does ── */}
            <div {...stylex.props(sheet.main)}>
              <div {...stylex.props(typeCard.grid)} role="radiogroup" aria-label="Type">
                {TYPES.map((ty) => {
                  const on = t === ty.id;
                  return (
                    <button
                      key={ty.id}
                      type="button"
                      role="radio"
                      aria-checked={on}
                      {...stylex.props(typeCard.card, on && typeCard.on)}
                      onClick={() => set('input_type', ty.id)}
                      disabled={saving}
                    >
                      <span {...stylex.props(typeCard.head)}>
                        <span {...stylex.props(typeIcon.base, typeIcon[ty.id], typeCard.icon)}>
                          <TypeIcon type={ty.id} size={13} />
                        </span>
                        <span {...stylex.props(typeCard.label)}>{ty.label}</span>
                      </span>
                      <span {...stylex.props(typeCard.blurb)}>{ty.blurb}</span>
                    </button>
                  );
                })}
              </div>

              {agentic && (
                <div {...stylex.props(ed.block)}>
                  <label htmlFor="auto-prompt" {...stylex.props(field.label)}>
                    {t === 'monitor' ? 'What to watch' : 'Prompt'}
                  </label>
                  <textarea
                    id="auto-prompt"
                    {...stylex.props(field.textarea, ed.prompt, bad('prompt'))}
                    value={form.prompt_text ?? ''}
                    onChange={(e) => set('prompt_text', e.target.value)}
                    placeholder={
                      t === 'monitor'
                        ? 'The latest stable release of X — tell me when a new version ships.'
                        : 'Find the five most important AI stories from the last 24 hours and summarise each in two sentences.'
                    }
                    aria-invalid={error?.field === 'prompt'}
                    disabled={saving}
                  />
                  <span {...stylex.props(field.hint)}>
                    {t === 'monitor'
                      ? 'Each run sees what earlier checks found, and only notifies when something changed.'
                      : 'Written as you would ask in chat. The agent has its tools, skills and memory.'}
                  </span>
                </div>
              )}

              {t === 'code' && (
                <div {...stylex.props(ed.block)}>
                  <label htmlFor="auto-code" {...stylex.props(field.label)}>
                    Python
                  </label>
                  <textarea
                    id="auto-code"
                    {...stylex.props(field.textarea, ed.code, bad('code'))}
                    value={form.code_text ?? ''}
                    onChange={(e) => set('code_text', e.target.value)}
                    placeholder={'import datetime\nprint(datetime.datetime.now())'}
                    aria-invalid={error?.field === 'code'}
                    disabled={saving}
                    spellCheck={false}
                  />
                  <span {...stylex.props(field.hint)}>
                    Runs in a notebook kernel with the jarvis SDK loaded. What it prints is the
                    run's output.
                  </span>
                </div>
              )}

              {t === 'webhook' && (
                <>
                  <div {...stylex.props(ed.block)}>
                    <label htmlFor="auto-url" {...stylex.props(field.label)}>
                      Request
                    </label>
                    <div {...stylex.props(ed.request)}>
                      <select
                        {...stylex.props(field.select, field.selectChrome, ed.method)}
                        value={form.webhook_method ?? 'POST'}
                        onChange={(e) => set('webhook_method', e.target.value)}
                        disabled={saving}
                        aria-label="Method"
                      >
                        <option value="POST">POST</option>
                        <option value="GET">GET</option>
                        <option value="PUT">PUT</option>
                        <option value="DELETE">DELETE</option>
                      </select>
                      <input
                        id="auto-url"
                        type="url"
                        {...stylex.props(field.input, ed.url, bad('url'))}
                        value={form.webhook_url ?? ''}
                        onChange={(e) => set('webhook_url', e.target.value)}
                        placeholder="https://your-n8n.example.com/webhook/…"
                        aria-invalid={error?.field === 'url'}
                        disabled={saving}
                        spellCheck={false}
                      />
                    </div>
                  </div>
                  <div {...stylex.props(ed.block)}>
                    <label htmlFor="auto-headers" {...stylex.props(field.label)}>
                      Headers <span {...stylex.props(ed.labelNote)}>JSON</span>
                    </label>
                    <textarea
                      id="auto-headers"
                      {...stylex.props(field.textarea, ed.mono)}
                      rows={4}
                      value={form.webhook_headers ?? ''}
                      onChange={(e) => set('webhook_headers', e.target.value)}
                      placeholder={'{"Authorization": "Bearer …"}'}
                      disabled={saving}
                      spellCheck={false}
                    />
                  </div>
                  <div {...stylex.props(ed.block)}>
                    <label htmlFor="auto-body" {...stylex.props(field.label)}>
                      Body
                    </label>
                    <textarea
                      id="auto-body"
                      {...stylex.props(field.textarea, ed.mono)}
                      rows={8}
                      value={form.webhook_body ?? ''}
                      onChange={(e) => set('webhook_body', e.target.value)}
                      placeholder={'{"key": "value"}'}
                      disabled={saving}
                      spellCheck={false}
                    />
                  </div>
                </>
              )}
            </div>

            {/* ── When, with what, and who hears ── */}
            <aside {...stylex.props(sheet.rail)}>
              <section {...stylex.props(ed.card)}>
                <h3 {...stylex.props(ed.cardTitle)}>Schedule</h3>
                <ScheduleEditor
                  value={form.schedule ?? ''}
                  onChange={(cron) => set('schedule', cron)}
                  disabled={saving}
                />
              </section>

              {agentic && (
                <section {...stylex.props(ed.card)}>
                  <h3 {...stylex.props(ed.cardTitle)}>Agent</h3>
                  <div {...stylex.props(field.group)}>
                    <label htmlFor="auto-model" {...stylex.props(ed.smallLabel)}>
                      Model
                    </label>
                    <select
                      id="auto-model"
                      {...stylex.props(field.select, field.selectChrome)}
                      value={form.model ?? catalog?.default ?? ''}
                      onChange={(e) => set('model', e.target.value)}
                      disabled={saving || !catalog}
                    >
                      {!catalog && <option value="">Loading…</option>}
                      {catalog &&
                        form.model &&
                        !catalog.available.some((m) => m.id === form.model) && (
                          <option value={form.model}>{form.model}</option>
                        )}
                      {catalog?.available.map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.label}
                          {m.id === catalog.default ? ' — default' : ''}
                        </option>
                      ))}
                    </select>
                  </div>
                  {t === 'prompt' && (
                    <div {...stylex.props(ed.option)}>
                      <Switch
                        checked={form.stateful ?? false}
                        onChange={(v) => set('stateful', v)}
                        disabled={saving}
                        label={<span {...stylex.props(ed.optionLabel)}>Remember earlier runs</span>}
                      />
                      <span {...stylex.props(ed.optionHint)}>
                        Every run continues one conversation, so the agent sees what it said last
                        time.
                      </span>
                    </div>
                  )}
                </section>
              )}

              <section {...stylex.props(ed.card)}>
                <NotificationsEditor
                  value={notifications}
                  onChange={setNotifications}
                  disabled={saving}
                />
              </section>
            </aside>
          </div>
        </div>

        <footer {...stylex.props(sheet.footer)}>
          <div {...stylex.props(sheet.footerStatus)} role="status" aria-live="polite">
            {error ? (
              <span {...stylex.props(sheet.footerError)}>{error.message}</span>
            ) : dirty ? (
              <span>Unsaved changes</span>
            ) : null}
          </div>
          <span {...stylex.props(sheet.kbd)}>{mac ? '⌘' : 'Ctrl'} ↵</span>
          <button
            type="button"
            {...stylex.props(btn.base)}
            onClick={requestClose}
            disabled={saving}
          >
            Cancel
          </button>
          <button
            type="submit"
            {...stylex.props(btn.base, btn.primary, sheet.save)}
            disabled={saving || (!!editing && !dirty)}
          >
            {saving ? 'Saving…' : editing ? 'Save changes' : 'Create automation'}
          </button>
        </footer>
      </form>

      <ConfirmDialog
        open={confirmDiscard}
        title="Discard changes?"
        message="Your edits to this automation haven't been saved."
        confirmLabel="Discard"
        cancelLabel="Keep editing"
        danger
        onConfirm={onClose}
        onCancel={() => setConfirmDiscard(false)}
      />
    </>
  );
}
