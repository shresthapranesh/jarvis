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
    return parsed.filter(
      (c): c is NotificationConfig =>
        c && typeof c === 'object' && c.type === 'telegram' && typeof c.chat_id === 'string',
    );
  } catch {
    return [];
  }
}

export function serializeNotifications(rows: NotificationConfig[]): string | null {
  const cleaned = rows.filter((r) => r.chat_id.trim());
  return cleaned.length ? JSON.stringify(cleaned) : null;
}

export function NotificationsEditor({value, onChange, disabled}: Props) {
  function update(idx: number, patch: Partial<NotificationConfig>) {
    onChange(value.map((row, i) => (i === idx ? {...row, ...patch} : row)));
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
          No notifications. Add one to get a Telegram message when this finishes.
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
            onChange={(e) => update(idx, {type: e.target.value as NotificationConfig['type']})}
            disabled={disabled}
            style={{flex: '0 0 110px'}}
          >
            <option value="telegram">Telegram</option>
          </select>
          <input
            className="auto-form-input"
            value={row.chat_id}
            onChange={(e) => update(idx, {chat_id: e.target.value})}
            placeholder="chat id (e.g. 123456789)"
            disabled={disabled}
            style={{flex: 1}}
          />
          <select
            className="auto-form-select"
            value={row.on}
            onChange={(e) => update(idx, {on: e.target.value as NotificationOn})}
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
