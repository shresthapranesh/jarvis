import {useQuery} from '@tanstack/react-query';
import {Link} from '@tanstack/react-router';

import {fetchNotificationChannels} from '../relay/NotificationChannelsQuery';
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
        c && typeof c === 'object' && typeof c.id === 'string',
    );
  } catch {
    return [];
  }
}

export function serializeNotifications(rows: NotificationConfig[]): string | null {
  const cleaned = rows.filter((r) => r.id.trim());
  return cleaned.length ? JSON.stringify(cleaned) : null;
}

export function NotificationsEditor({value, onChange, disabled}: Props) {
  const {data: channels = [], isLoading} = useQuery({
    queryKey: ['notification-channels'],
    queryFn: fetchNotificationChannels,
    staleTime: 30_000,
  });

  function update(idx: number, patch: Partial<NotificationConfig>) {
    onChange(value.map((row, i) => (i === idx ? {...row, ...patch} : row)));
  }
  function remove(idx: number) {
    onChange(value.filter((_, i) => i !== idx));
  }
  function add() {
    const firstId = channels[0]?.id ?? '';
    onChange([...value, {id: firstId, on: 'both'}]);
  }

  const noChannels = !isLoading && channels.length === 0;

  return (
    <div className="auto-form-group">
      <label className="auto-form-label">Notifications</label>
      {value.length === 0 && (
        <div style={{fontSize: '0.78rem', color: 'var(--text-dim)', marginBottom: 6}}>
          {noChannels ? (
            <>
              No channels configured.{' '}
              <Link to="/settings" style={{color: 'var(--accent)'}}>
                Add one in Settings
              </Link>{' '}
              to enable notifications.
            </>
          ) : (
            'No notifications. Add one to get a Telegram or Discord message when this finishes.'
          )}
        </div>
      )}
      {value.map((row, idx) => {
        const channel = channels.find((c) => c.id === row.id);
        const orphan = row.id && !channel && !isLoading;
        return (
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
              value={row.id}
              onChange={(e) => update(idx, {id: e.target.value})}
              disabled={disabled}
              style={{flex: 1}}
            >
              {orphan && (
                <option value={row.id}>
                  (missing channel {row.id.slice(0, 8)}…)
                </option>
              )}
              {channels.length === 0 && !orphan && (
                <option value="" disabled>
                  No channels — add in Settings
                </option>
              )}
              {channels.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name} ({c.type})
                </option>
              ))}
            </select>
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
        );
      })}
      <button
        type="button"
        className="activity-btn"
        onClick={add}
        disabled={disabled || channels.length === 0}
        style={{marginTop: 4}}
        title={channels.length === 0 ? 'Add a channel in Settings first' : undefined}
      >
        + Add notification
      </button>
    </div>
  );
}
