import type {NotificationConfig, NotificationOn} from '../lib/types';

interface Props {
  value: NotificationConfig[];
  onChange: (next: NotificationConfig[]) => void;
  disabled?: boolean;
}

export function parseNotifications(raw: string | null | undefined): NotificationConfig[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter((c): c is NotificationConfig => {
      if (!c || typeof c !== 'object') return false;
      if (c.type === 'telegram' && typeof c.chat_id === 'string') return true;
      if (c.type === 'discord' && typeof c.channel_id === 'string') return true;
      return false;
    });
  } catch {
    return [];
  }
}

export function serializeNotifications(rows: NotificationConfig[]): string | null {
  const cleaned = rows.filter((r) =>
    r.type === 'telegram' ? r.chat_id.trim() : r.channel_id.trim(),
  );
  return cleaned.length ? JSON.stringify(cleaned) : null;
}

function rowId(row: NotificationConfig): string {
  return row.type === 'telegram' ? row.chat_id : row.channel_id;
}

function setRowId(row: NotificationConfig, id: string): NotificationConfig {
  return row.type === 'telegram'
    ? {type: 'telegram', chat_id: id, on: row.on}
    : {type: 'discord', channel_id: id, on: row.on};
}

function setRowType(row: NotificationConfig, type: NotificationConfig['type']): NotificationConfig {
  const id = rowId(row);
  return type === 'telegram'
    ? {type: 'telegram', chat_id: id, on: row.on}
    : {type: 'discord', channel_id: id, on: row.on};
}

function setRowOn(row: NotificationConfig, on: NotificationOn): NotificationConfig {
  return row.type === 'telegram'
    ? {type: 'telegram', chat_id: row.chat_id, on}
    : {type: 'discord', channel_id: row.channel_id, on};
}

export function NotificationsEditor({value, onChange, disabled}: Props) {
  function replace(idx: number, next: NotificationConfig) {
    onChange(value.map((row, i) => (i === idx ? next : row)));
  }
  function remove(idx: number) {
    onChange(value.filter((_, i) => i !== idx));
  }
  function add() {
    onChange([...value, {type: 'telegram', chat_id: '', on: 'both'}]);
  }

  return (
    <div className="auto-form-group">
      <label className="auto-form-label">Notifications</label>
      {value.length === 0 && (
        <div style={{fontSize: '0.78rem', color: 'var(--text-dim)', marginBottom: 6}}>
          No notifications. Add one to get a Telegram or Discord message when this finishes.
        </div>
      )}
      {value.map((row, idx) => (
        <div
          key={idx}
          style={{
            display: 'flex',
            gap: 6,
            marginBottom: 6,
            alignItems: 'center',
          }}
        >
          <select
            className="auto-form-select"
            value={row.type}
            onChange={(e) =>
              replace(idx, setRowType(row, e.target.value as NotificationConfig['type']))
            }
            disabled={disabled}
            style={{flex: '0 0 110px'}}
          >
            <option value="telegram">Telegram</option>
            <option value="discord">Discord</option>
          </select>
          <input
            className="auto-form-input"
            value={rowId(row)}
            onChange={(e) => replace(idx, setRowId(row, e.target.value))}
            placeholder={
              row.type === 'telegram'
                ? 'chat id (e.g. 123456789)'
                : 'channel id (e.g. 1234567890123456789)'
            }
            disabled={disabled}
            style={{flex: 1}}
          />
          <select
            className="auto-form-select"
            value={row.on}
            onChange={(e) => replace(idx, setRowOn(row, e.target.value as NotificationOn))}
            disabled={disabled}
            style={{flex: '0 0 110px'}}
          >
            <option value="both">on: both</option>
            <option value="done">on: done</option>
            <option value="error">on: error</option>
          </select>
          <button
            type="button"
            className="activity-btn"
            onClick={() => remove(idx)}
            disabled={disabled}
            style={{flex: '0 0 auto'}}
          >
            Remove
          </button>
        </div>
      ))}
      <button
        type="button"
        className="activity-btn"
        onClick={add}
        disabled={disabled}
        style={{marginTop: 4}}
      >
        + Add notification
      </button>
    </div>
  );
}
