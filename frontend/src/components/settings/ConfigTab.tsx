import * as stylex from '@stylexjs/stylex';
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
import {badge, btn, codeField, field, page} from '../ui';
// `settings` and `tools` are also local variable names in these tabs.
import {configNew, settings as sx, tools as toolStyles} from './settings.styles';

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
    <div {...stylex.props(page.section)}>
      <h2 {...stylex.props(page.sectionTitle)}>
        Config <span {...stylex.props(page.count)}>{settings.length}</span>
        <span {...stylex.props(page.sectionHint)}>
          {setCount} set · the same rows as <code>main.py config list</code>
        </span>
      </h2>

      <div {...stylex.props(sx.filterRow)}>
        <div {...stylex.props(sx.search)}>
          <SearchIcon size={14} />
          <input
            {...stylex.props(sx.searchInput)}
            placeholder="Search keys…"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
        </div>
        <label {...stylex.props(toolStyles.toggle)} title="Show keys owned by another Settings tab">
          <input
            type="checkbox"
            checked={showManaged}
            onChange={(e) => setShowManaged(e.target.checked)}
          />
          <span>Show managed keys</span>
        </label>
        <button {...stylex.props(btn.base)} onClick={() => setAdding(true)}>
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
        <div {...stylex.props(page.empty)}>No settings match the filter.</div>
      ) : (
        <ul {...stylex.props(toolStyles.list)}>
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
    <li
      {...stylex.props(toolStyles.row, toolStyles.rowStacked, !setting.isSet && toolStyles.rowOff)}
    >
      <div {...stylex.props(toolStyles.rowMain)}>
        <div {...stylex.props(toolStyles.rowHead)}>
          <span {...stylex.props(toolStyles.rowName)}>{setting.label}</span>
          <code {...stylex.props(codeField.key)}>{setting.key}</code>
          {managed && (
            <span
              {...stylex.props(badge.base)}
              title={`Edited on the ${setting.managedBy} tab, which rewrites this key wholesale.`}
            >
              managed by {setting.managedBy}
            </span>
          )}
          {setting.restartRequired && (
            <span
              {...stylex.props(badge.base)}
              title="Read once at startup — a change here is stored but not applied until the server restarts."
            >
              restart required
            </span>
          )}
          {!setting.known && <span {...stylex.props(badge.base)}>custom key</span>}
          {!setting.isSet && <span {...stylex.props(badge.base)}>unset</span>}
        </div>
        {setting.description && <p {...stylex.props(toolStyles.rowDesc)}>{setting.description}</p>}

        {managed ? (
          <pre {...stylex.props(codeField.readonly)}>{setting.value || '(unset)'}</pre>
        ) : (
          <div {...stylex.props(codeField.edit)}>
            {setting.kind === 'select' ? (
              <select
                {...stylex.props(field.select, field.selectChrome)}
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
                {...stylex.props(codeField.input, codeField.multiline)}
                rows={4}
                value={draft}
                disabled={busy}
                placeholder={setting.placeholder}
                onChange={(e) => setDraft(e.target.value)}
              />
            ) : (
              <input
                {...stylex.props(codeField.input)}
                value={draft}
                disabled={busy}
                placeholder={setting.placeholder}
                onChange={(e) => setDraft(e.target.value)}
              />
            )}
            <div {...stylex.props(codeField.actions)}>
              <button
                {...stylex.props(btn.base, btn.primary)}
                disabled={busy || !dirty}
                onClick={() => void save()}
              >
                Save
              </button>
              <button
                {...stylex.props(btn.base)}
                disabled={busy || !setting.isSet}
                title="Delete the row — the key reverts to its built-in default."
                onClick={() => setConfirmClear(true)}
              >
                Clear
              </button>
              {dirty && <span {...stylex.props(codeField.hint)}>unsaved</span>}
            </div>
          </div>
        )}
        {setting.updatedAt && (
          <p {...stylex.props(codeField.meta)}>
            Updated {new Date(setting.updatedAt).toLocaleString()}
          </p>
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
    <div {...stylex.props(configNew.root)}>
      <input
        {...stylex.props(codeField.input)}
        placeholder="key (e.g. telegram.allowed_users)"
        value={key}
        disabled={busy}
        onChange={(e) => setKey(e.target.value)}
      />
      <input
        {...stylex.props(codeField.input)}
        placeholder="value"
        value={value}
        disabled={busy}
        onChange={(e) => setValue(e.target.value)}
      />
      <button
        {...stylex.props(btn.base, btn.primary)}
        disabled={busy || !key.trim()}
        onClick={() => void save()}
      >
        Save
      </button>
      <button {...stylex.props(btn.base)} disabled={busy} onClick={onCancel}>
        Cancel
      </button>
    </div>
  );
}
