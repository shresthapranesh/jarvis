import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {SettingsQuery as TSettingsQuery} from '../../__generated__/SettingsQuery.graphql';
import {useToast} from '../../lib/toast';
import {commitDeleteSetting} from '../../relay/DeleteSettingMutation';
import {commitSetSetting} from '../../relay/SetSettingMutation';
import {settingsQuery} from '../../relay/SettingsQuery';
import {ConfirmDialog} from '../ConfirmDialog';
import {SearchIcon} from '../icons';
import {useQueryRetry} from '../QueryBoundary';

type Setting = TSettingsQuery['response']['settings'][number];

// The generic editor over `config_settings` — the table `main.py config
// set/get/list/delete` writes. Keys another tab owns are shown read-only:
// they hold serialized state that tab rewrites wholesale, so a hand edit here
// is discarded the next time it writes.
export function ConfigTab() {
  const toast = useToast();
  const data = useLazyLoadQuery<TSettingsQuery>(
    settingsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );

  const [filter, setFilter] = useState('');
  const [showManaged, setShowManaged] = useState(false);
  const [adding, setAdding] = useState(false);

  const settings = data.settings;
  const setCount = settings.filter((s) => s.isSet).length;

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase();
    return settings.filter((s) => {
      if (!showManaged && s.managedBy) return false;
      if (!q) return true;
      return (
        s.key.toLowerCase().includes(q) ||
        s.label.toLowerCase().includes(q) ||
        s.description.toLowerCase().includes(q)
      );
    });
  }, [settings, filter, showManaged]);

  return (
    <div className="memory-section">
      <h2 className="memory-section-title">
        Config <span className="memory-count">{settings.length}</span>
        <span className="memory-section-hint">
          {setCount} set · the same rows as <code>main.py config list</code>
        </span>
      </h2>

      <div className="settings-filter-row">
        <div className="settings-search">
          <SearchIcon size={14} />
          <input
            placeholder="Search keys…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <label className="tool-toggle" title="Show keys owned by another Settings tab">
          <input
            type="checkbox"
            checked={showManaged}
            onChange={(e) => setShowManaged(e.target.checked)}
          />
          <span>Show managed keys</span>
        </label>
        <button className="artifact-btn" onClick={() => setAdding(true)}>
          Add key
        </button>
      </div>

      {adding && (
        <NewKeyRow
          onCancel={() => setAdding(false)}
          onSaved={(note) => {
            setAdding(false);
            toast.push(note, 'success');
          }}
          onError={(msg) => toast.push(msg, 'error')}
        />
      )}

      {filtered.length === 0 ? (
        <div className="memory-empty">No settings match the filter.</div>
      ) : (
        <ul className="tool-list">
          {filtered.map((s) => (
            <SettingRow
              key={s.key}
              setting={s}
              onSaved={(note) => toast.push(note, 'success')}
              onError={(msg) => toast.push(msg, 'error')}
            />
          ))}
        </ul>
      )}
    </div>
  );
}

function SettingRow({
  setting,
  onSaved,
  onError,
}: {
  setting: Setting;
  onSaved: (note: string) => void;
  onError: (msg: string) => void;
}) {
  const [draft, setDraft] = useState(setting.value);
  const [busy, setBusy] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false);
  const managed = Boolean(setting.managedBy);
  const dirty = draft !== setting.value;

  async function save() {
    setBusy(true);
    try {
      const res = await commitSetSetting({key: setting.key, value: draft, allowManaged: false});
      onSaved(`${setting.key}: ${res.note}`);
    } catch (e) {
      onError((e as Error).message || String(e));
      setDraft(setting.value);
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    setBusy(true);
    try {
      const res = await commitDeleteSetting({key: setting.key, allowManaged: false});
      setDraft('');
      onSaved(`${setting.key}: ${res.note}`);
    } catch (e) {
      onError((e as Error).message || String(e));
    } finally {
      setBusy(false);
      setConfirmClear(false);
    }
  }

  const multiline = setting.kind === 'json' || setting.value.length > 80;

  return (
    <li className={`tool-row config-row${setting.isSet ? '' : ' tool-row--off'}`}>
      <div className="tool-row-main">
        <div className="tool-row-head">
          <span className="tool-row-name">{setting.label}</span>
          <code className="config-key">{setting.key}</code>
          {managed && (
            <span
              className="settings-badge"
              title={`Edited on the ${setting.managedBy} tab, which rewrites this key wholesale.`}
            >
              managed by {setting.managedBy}
            </span>
          )}
          {setting.restartRequired && (
            <span
              className="settings-badge"
              title="Read once at startup — a change here is stored but not applied until the server restarts."
            >
              restart required
            </span>
          )}
          {!setting.known && <span className="settings-badge">custom key</span>}
          {!setting.isSet && <span className="settings-badge">unset</span>}
        </div>
        {setting.description && <p className="tool-row-desc">{setting.description}</p>}

        {managed ? (
          <pre className="config-readonly">{setting.value || '(unset)'}</pre>
        ) : (
          <div className="config-edit">
            {setting.kind === 'select' ? (
              <select
                className="auto-form-select"
                value={draft}
                disabled={busy}
                onChange={(e) => setDraft(e.target.value)}
              >
                <option value="">(unset)</option>
                {setting.choices.map((c) => (
                  <option key={c} value={c}>
                    {c}
                  </option>
                ))}
              </select>
            ) : multiline ? (
              <textarea
                className="config-input config-input--multiline"
                rows={4}
                value={draft}
                disabled={busy}
                placeholder={setting.placeholder}
                onChange={(e) => setDraft(e.target.value)}
              />
            ) : (
              <input
                className="config-input"
                value={draft}
                disabled={busy}
                placeholder={setting.placeholder}
                onChange={(e) => setDraft(e.target.value)}
              />
            )}
            <div className="config-actions">
              <button
                className="artifact-btn primary"
                disabled={busy || !dirty}
                onClick={() => void save()}
              >
                Save
              </button>
              <button
                className="artifact-btn"
                disabled={busy || !setting.isSet}
                title="Delete the row — the key reverts to its built-in default."
                onClick={() => setConfirmClear(true)}
              >
                Clear
              </button>
              {dirty && <span className="config-hint">unsaved</span>}
            </div>
          </div>
        )}
        {setting.updatedAt && (
          <p className="config-meta">Updated {new Date(setting.updatedAt).toLocaleString()}</p>
        )}
      </div>

      <ConfirmDialog
        open={confirmClear}
        title={`Clear ${setting.key}?`}
        message="The row is deleted and the key reverts to its built-in default."
        confirmLabel="Clear"
        danger
        onConfirm={() => void clear()}
        onCancel={() => setConfirmClear(false)}
      />
    </li>
  );
}

// Free-form keys are the point of keeping this table open-ended — the CLI never
// validated them either, and refusing them would make this tab strictly less
// capable than the command it replaces.
function NewKeyRow({
  onCancel,
  onSaved,
  onError,
}: {
  onCancel: () => void;
  onSaved: (note: string) => void;
  onError: (msg: string) => void;
}) {
  const [key, setKey] = useState('');
  const [value, setValue] = useState('');
  const [busy, setBusy] = useState(false);

  async function save() {
    setBusy(true);
    try {
      const res = await commitSetSetting({key: key.trim(), value, allowManaged: false});
      onSaved(`${key.trim()}: ${res.note}`);
    } catch (e) {
      onError((e as Error).message || String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="config-new">
      <input
        className="config-input"
        placeholder="key (e.g. telegram.allowed_users)"
        value={key}
        disabled={busy}
        onChange={(e) => setKey(e.target.value)}
      />
      <input
        className="config-input"
        placeholder="value"
        value={value}
        disabled={busy}
        onChange={(e) => setValue(e.target.value)}
      />
      <button
        className="artifact-btn primary"
        disabled={busy || !key.trim()}
        onClick={() => void save()}
      >
        Save
      </button>
      <button className="artifact-btn" disabled={busy} onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}
