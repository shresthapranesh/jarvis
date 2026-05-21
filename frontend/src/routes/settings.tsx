import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {createFileRoute} from '@tanstack/react-router';
import {useState} from 'react';

import type {NotificationChannelInput} from '../lib/api';
import {commitCreateNotificationChannel} from '../relay/CreateNotificationChannelMutation';
import {commitDeleteNotificationChannel} from '../relay/DeleteNotificationChannelMutation';
import {fetchNotificationChannels} from '../relay/NotificationChannelsQuery';
import {commitUpdateNotificationChannel} from '../relay/UpdateNotificationChannelMutation';
import {useToast} from '../lib/toast';
import type {
  NotificationChannel,
  NotificationChannelReference,
  NotificationChannelType,
} from '../lib/types';

export const Route = createFileRoute('/settings')({component: SettingsPage});

function SettingsPage() {
  return (
    <div className="tasks-page">
      <div className="auto-page-header">
        <div className="auto-page-titlerow">
          <h1 style={{margin: 0, fontSize: '1.4rem'}}>Settings</h1>
        </div>
        <div style={{fontSize: '0.85rem', color: 'var(--text-dim)'}}>
          Define notification channels once here, then pick them by name when
          configuring an automation or workflow.
        </div>
      </div>
      <div style={{padding: '20px 28px', overflow: 'auto'}}>
        <NotificationChannelsSection />
      </div>
    </div>
  );
}

function NotificationChannelsSection() {
  const queryClient = useQueryClient();
  const {data: channels = [], isLoading} = useQuery({
    queryKey: ['notification-channels'],
    queryFn: fetchNotificationChannels,
  });
  const [draft, setDraft] = useState<NotificationChannelInput | null>(null);

  function refresh() {
    queryClient.invalidateQueries({queryKey: ['notification-channels']});
  }

  return (
    <section style={{maxWidth: 720}}>
      <h2 style={{fontSize: '1rem', margin: '0 0 10px'}}>Notification Channels</h2>
      {isLoading && <div style={{color: 'var(--text-dim)'}}>Loading…</div>}
      {!isLoading && channels.length === 0 && draft === null && (
        <div style={{color: 'var(--text-dim)', fontSize: '0.85rem', marginBottom: 10}}>
          No channels yet. Add one below.
        </div>
      )}

      <div style={{display: 'flex', flexDirection: 'column', gap: 8}}>
        {channels.map((ch) => (
          <ChannelRow key={ch.id} channel={ch} onChanged={refresh} />
        ))}
        {draft !== null && (
          <DraftRow
            draft={draft}
            onChange={setDraft}
            onCancel={() => setDraft(null)}
            onSaved={() => {
              setDraft(null);
              refresh();
            }}
          />
        )}
      </div>

      <button
        type="button"
        className="activity-btn"
        onClick={() =>
          setDraft({name: '', type: 'telegram', target: ''})
        }
        disabled={draft !== null}
        style={{marginTop: 12}}
      >
        + Add channel
      </button>
    </section>
  );
}

interface ChannelRowProps {
  channel: NotificationChannel;
  onChanged: () => void;
}

function ChannelRow({channel, onChanged}: ChannelRowProps) {
  const toast = useToast();
  const [editing, setEditing] = useState(false);
  const [name, setName] = useState(channel.name);
  const [type, setType] = useState<NotificationChannelType>(channel.type);
  const [target, setTarget] = useState(channel.target);
  const [refsInUse, setRefsInUse] = useState<NotificationChannelReference[] | null>(null);

  const updateMut = useMutation({
    mutationFn: () =>
      commitUpdateNotificationChannel(channel.id, {name: name.trim(), type, target: target.trim()}),
    onSuccess: () => {
      toast.push('Channel updated', 'success');
      setEditing(false);
      onChanged();
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  });

  const deleteMut = useMutation({
    mutationFn: () => commitDeleteNotificationChannel(channel.id),
    onSuccess: (result) => {
      if (result.ok) {
        toast.push('Channel deleted', 'success');
        onChanged();
      } else {
        setRefsInUse(result.references ?? []);
      }
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  });

  if (!editing) {
    return (
      <div
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 10,
          padding: '8px 10px',
          border: '1px solid var(--border)',
          borderRadius: 6,
        }}
      >
        <div style={{flex: 1, minWidth: 0}}>
          <div style={{fontWeight: 500}}>{channel.name}</div>
          <div style={{fontSize: '0.78rem', color: 'var(--text-dim)'}}>
            {channel.type} — {channel.target}
          </div>
          {refsInUse && refsInUse.length > 0 && (
            <div style={{fontSize: '0.78rem', color: 'var(--err, #c45)', marginTop: 4}}>
              Cannot delete — used by:{' '}
              {refsInUse.map((r, i) => (
                <span key={`${r.kind}:${r.id}`}>
                  {i > 0 && ', '}
                  {r.kind} "{r.name}"
                </span>
              ))}
            </div>
          )}
        </div>
        <button
          type="button"
          className="activity-btn"
          onClick={() => setEditing(true)}
        >
          Edit
        </button>
        <button
          type="button"
          className="activity-btn"
          onClick={() => deleteMut.mutate()}
          disabled={deleteMut.isPending}
        >
          {deleteMut.isPending ? 'Deleting…' : 'Delete'}
        </button>
      </div>
    );
  }

  return (
    <RowForm
      name={name}
      type={type}
      target={target}
      onNameChange={setName}
      onTypeChange={setType}
      onTargetChange={setTarget}
      onSave={() => updateMut.mutate()}
      onCancel={() => {
        setName(channel.name);
        setType(channel.type);
        setTarget(channel.target);
        setEditing(false);
      }}
      saving={updateMut.isPending}
      saveLabel="Save"
    />
  );
}

interface DraftRowProps {
  draft: NotificationChannelInput;
  onChange: (next: NotificationChannelInput) => void;
  onCancel: () => void;
  onSaved: () => void;
}

function DraftRow({draft, onChange, onCancel, onSaved}: DraftRowProps) {
  const toast = useToast();
  const createMut = useMutation({
    mutationFn: () =>
      commitCreateNotificationChannel({
        name: draft.name.trim(),
        type: draft.type,
        target: draft.target.trim(),
      }),
    onSuccess: () => {
      toast.push('Channel created', 'success');
      onSaved();
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  });

  return (
    <RowForm
      name={draft.name}
      type={draft.type}
      target={draft.target}
      onNameChange={(name) => onChange({...draft, name})}
      onTypeChange={(type) => onChange({...draft, type})}
      onTargetChange={(target) => onChange({...draft, target})}
      onSave={() => createMut.mutate()}
      onCancel={onCancel}
      saving={createMut.isPending}
      saveLabel="Create"
    />
  );
}

interface RowFormProps {
  name: string;
  type: NotificationChannelType;
  target: string;
  onNameChange: (name: string) => void;
  onTypeChange: (type: NotificationChannelType) => void;
  onTargetChange: (target: string) => void;
  onSave: () => void;
  onCancel: () => void;
  saving: boolean;
  saveLabel: string;
}

function RowForm({
  name,
  type,
  target,
  onNameChange,
  onTypeChange,
  onTargetChange,
  onSave,
  onCancel,
  saving,
  saveLabel,
}: RowFormProps) {
  const disabled = !name.trim() || !target.trim() || saving;
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        padding: 12,
        border: '1px solid var(--border)',
        borderRadius: 6,
        background: 'var(--surface-2, transparent)',
      }}
    >
      <div style={{display: 'flex', gap: 8}}>
        <input
          className="auto-form-input"
          value={name}
          onChange={(e) => onNameChange(e.target.value)}
          placeholder="Channel name (e.g. team-discord)"
          disabled={saving}
          style={{flex: 1}}
        />
        <select
          className="auto-form-select"
          value={type}
          onChange={(e) => onTypeChange(e.target.value as NotificationChannelType)}
          disabled={saving}
          style={{flex: '0 0 120px'}}
        >
          <option value="telegram">Telegram</option>
          <option value="discord">Discord</option>
        </select>
      </div>
      <input
        className="auto-form-input"
        value={target}
        onChange={(e) => onTargetChange(e.target.value)}
        placeholder={
          type === 'telegram'
            ? 'Telegram chat ID (e.g. 123456789)'
            : 'Discord channel ID (e.g. 1234567890123456789)'
        }
        disabled={saving}
      />
      <div style={{display: 'flex', gap: 8, justifyContent: 'flex-end'}}>
        <button
          type="button"
          className="auto-form-cancel-btn"
          onClick={onCancel}
          disabled={saving}
        >
          Cancel
        </button>
        <button
          type="button"
          className="auto-form-save-btn"
          onClick={onSave}
          disabled={disabled}
        >
          {saving ? 'Saving…' : saveLabel}
        </button>
      </div>
    </div>
  );
}
