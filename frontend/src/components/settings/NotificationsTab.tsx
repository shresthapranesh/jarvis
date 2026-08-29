import * as stylex from '@stylexjs/stylex';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {NotificationChannelsQuery as TNotificationChannelsQuery} from '../../__generated__/NotificationChannelsQuery.graphql';
import {useAsyncAction} from '../../hooks/useAsyncAction';
import type {NotificationChannelInput} from '../../lib/api';
import {useToast} from '../../lib/toast';
import type {
  NotificationChannel,
  NotificationChannelReference,
  NotificationChannelType,
} from '../../lib/types';
import {commitCreateNotificationChannel} from '../../relay/CreateNotificationChannelMutation';
import {commitDeleteNotificationChannel} from '../../relay/DeleteNotificationChannelMutation';
import {
  mapChannel,
  notificationChannelsQuery,
  refreshNotificationChannels,
} from '../../relay/NotificationChannelsQuery';
import {commitUpdateNotificationChannel} from '../../relay/UpdateNotificationChannelMutation';
import {ConfirmDialog} from '../ConfirmDialog';
import {FormModal} from '../FormModal';
import {EditIcon, PlusIcon, TrashIcon} from '../icons';
import {item} from '../memory.styles';
import {useQueryRetry} from '../QueryBoundary';
import {badge, btn, field, iconBtn, page} from '../ui';
import {settings} from './settings.styles';

interface ChannelDraft {
  name: string;
  type: NotificationChannelType;
  target: string;
}

const EMPTY_CHANNEL: ChannelDraft = {name: '', type: 'telegram', target: ''};

type ChannelEditor = {mode: 'add'} | {mode: 'edit'; channel: NotificationChannel};

export function NotificationsTab() {
  const toast = useToast();
  const channelData = useLazyLoadQuery<TNotificationChannelsQuery>(
    notificationChannelsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const channels = useMemo(
    () => channelData.notificationChannels.map(mapChannel),
    [channelData.notificationChannels],
  );

  const [editor, setEditor] = useState<ChannelEditor | null>(null);
  const [draft, setDraft] = useState<ChannelDraft>(EMPTY_CHANNEL);
  const [actionError, setActionError] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<NotificationChannel | null>(null);
  const [refsInUse, setRefsInUse] = useState<NotificationChannelReference[]>([]);

  const invalidate = refreshNotificationChannels;

  function openAdd() {
    setDraft(EMPTY_CHANNEL);
    setActionError(null);
    setEditor({mode: 'add'});
  }

  function openEdit(ch: NotificationChannel) {
    setDraft({name: ch.name, type: ch.type, target: ch.target});
    setActionError(null);
    setEditor({mode: 'edit', channel: ch});
  }

  function closeEditor() {
    setEditor(null);
    setActionError(null);
  }

  const input: NotificationChannelInput = {
    name: draft.name.trim(),
    type: draft.type,
    target: draft.target.trim(),
  };

  const createMut = useAsyncAction(
    async () => {
      await commitCreateNotificationChannel(input);
      toast.push('Channel created', 'success');
      await invalidate();
    },
    {onSuccess: closeEditor, onError: (e) => setActionError(e.message)},
  );

  const updateMut = useAsyncAction(
    async (id: string) => {
      await commitUpdateNotificationChannel(id, input);
      toast.push('Channel updated', 'success');
      await invalidate();
    },
    {onSuccess: closeEditor, onError: (e) => setActionError(e.message)},
  );

  const deleteMut = useAsyncAction(
    async (id: string) => {
      const result = await commitDeleteNotificationChannel(id);
      setDeleteTarget(null);
      if (result.ok) {
        toast.push('Channel deleted', 'success');
        setRefsInUse([]);
        await invalidate();
      } else {
        // Still referenced by an automation/workflow — show what blocks it.
        setRefsInUse(result.references ?? []);
      }
    },
    {
      onError: (e) => {
        setDeleteTarget(null);
        toast.push(e.message, 'error');
      },
    },
  );

  const draftValid = Boolean(draft.name.trim() && draft.target.trim());

  return (
    <div {...stylex.props(page.section)}>
      <h2 {...stylex.props(page.sectionTitle)}>
        Channels <span {...stylex.props(page.count)}>{channels.length}</span>
        <span {...stylex.props(page.sectionHint)}>
          chat IDs from @userinfobot (Telegram) or Developer Mode → Copy ID (Discord)
        </span>
        <span {...stylex.props(settings.sectionActions)}>
          <button {...stylex.props(btn.base, btn.primary)} onClick={openAdd}>
            <PlusIcon size={14} /> New channel
          </button>
        </span>
      </h2>

      {refsInUse.length > 0 && (
        <div {...stylex.props(page.error)}>
          Cannot delete — still used by:{' '}
          {refsInUse.map((r, i) => (
            <span key={`${r.kind}:${r.id}`}>
              {i > 0 && ', '}
              {r.kind} "{r.name}"
            </span>
          ))}
        </div>
      )}

      {channels.length === 0 ? (
        <div {...stylex.props(page.empty)}>
          <p>No channels yet.</p>
          <p>Create one to get notified when automations finish, fail, or detect changes.</p>
          <button {...stylex.props(btn.base, btn.primary)} onClick={openAdd}>
            <PlusIcon size={14} /> New channel
          </button>
        </div>
      ) : (
        <ul {...stylex.props(page.list)}>
          {channels.map((ch) => (
            <li key={ch.id} {...stylex.props(item.root)}>
              <div {...stylex.props(item.main)}>
                <span {...stylex.props(item.text, settings.channelName)}>
                  <span
                    {...stylex.props(badge.base, ch.type === 'telegram' ? badge.http : badge.sse)}
                  >
                    {ch.type}
                  </span>
                  {ch.name}
                </span>
                <span {...stylex.props(item.meta)}>target: {ch.target}</span>
              </div>
              <div {...stylex.props(item.actions)}>
                <button
                  {...stylex.props(iconBtn.base)}
                  title="Edit channel"
                  onClick={() => openEdit(ch)}
                >
                  <EditIcon size={14} />
                </button>
                <button
                  {...stylex.props(iconBtn.base, iconBtn.danger)}
                  title="Delete channel"
                  onClick={() => setDeleteTarget(ch)}
                >
                  <TrashIcon size={14} />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <FormModal
        open={editor !== null}
        title={editor?.mode === 'edit' ? 'Edit channel' : 'New channel'}
        subtitle="Referenced by name from automations and workflows."
        submitLabel={editor?.mode === 'edit' ? 'Save changes' : 'Create channel'}
        submitDisabled={!draftValid}
        pending={createMut.pending || updateMut.pending}
        error={actionError}
        onSubmit={() => {
          if (!editor) return;
          if (editor.mode === 'add') void createMut.run();
          else void updateMut.run(editor.channel.id);
        }}
        onClose={closeEditor}
      >
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Name</span>
          <input
            {...stylex.props(field.input)}
            value={draft.name}
            onChange={(e) => setDraft({...draft, name: e.target.value})}
            autoFocus={editor?.mode === 'add'}
            spellCheck={false}
            placeholder="team-discord"
          />
        </div>
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Type</span>
          <select
            {...stylex.props(field.select, field.selectChrome)}
            value={draft.type}
            onChange={(e) => setDraft({...draft, type: e.target.value as NotificationChannelType})}
          >
            <option value="telegram">Telegram</option>
            <option value="discord">Discord</option>
          </select>
        </div>
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>
            Target — {draft.type === 'telegram' ? 'chat ID' : 'channel ID'}
          </span>
          <input
            {...stylex.props(field.input)}
            value={draft.target}
            onChange={(e) => setDraft({...draft, target: e.target.value})}
            spellCheck={false}
            placeholder={draft.type === 'telegram' ? '123456789' : '123456789012345678'}
          />
        </div>
      </FormModal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete channel"
        message={
          <p>
            Delete <strong>{deleteTarget?.name}</strong>? Automations referencing it will stop
            delivering.
          </p>
        }
        confirmLabel="Delete"
        danger
        onConfirm={() => deleteTarget && void deleteMut.run(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
