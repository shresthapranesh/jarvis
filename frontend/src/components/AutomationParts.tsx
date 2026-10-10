import * as stylex from '@stylexjs/stylex';
import {useEffect} from 'react';

import {useToast} from '../lib/toast';
import type {Automation, AutomationInputType, CreateAutomationPayload} from '../lib/types';
import {refreshAutomationList} from '../relay/AutomationListQuery';
import {commitCreateAutomation} from '../relay/CreateAutomationMutation';
import {commitUpdateAutomation} from '../relay/UpdateAutomationMutation';
import {panel, statusDot} from '../routes/automation.styles';
import {pill} from './AutomationDetail.styles';
import {AutomationForm} from './AutomationForm';
import {BoltIcon, CodeIcon, EyeIcon, WebhookIcon, XIcon} from './icons';
import {closeBtn} from './ui';

/* The pieces the automation list, detail and run pages share. */

export function TypeIcon({type, size = 14}: {type: AutomationInputType; size?: number}) {
  if (type === 'prompt') return <BoltIcon size={size} />;
  if (type === 'code') return <CodeIcon size={size} />;
  if (type === 'monitor') return <EyeIcon size={size} />;
  return <WebhookIcon size={size} />;
}

// Run status is server data, so the variant is looked up rather than indexed
// — an unknown status falls back to the base dot.
export function dotVariant(status: string | null | undefined) {
  if (status === 'done') return statusDot.done;
  if (status === 'error') return statusDot.error;
  if (status === 'running') return statusDot.running;
  if (status === 'no_change') return statusDot.no_change;
  if (status === 'skipped') return statusDot.skipped;
  if (status === 'stopped') return statusDot.stopped;
  if (status === 'blocked') return statusDot.blocked;
  return null;
}

/** The update payload that flips an automation's `enabled`, everything else as stored. */
export function toggledPayload(auto: Automation): CreateAutomationPayload {
  return {
    name: auto.name,
    description: auto.description,
    input_type: auto.input_type,
    prompt_text: auto.prompt_text,
    model: auto.model,
    code_text: auto.code_text,
    webhook_url: auto.webhook_url,
    webhook_method: auto.webhook_method,
    webhook_headers: auto.webhook_headers,
    webhook_body: auto.webhook_body,
    schedule: auto.schedule,
    enabled: !auto.enabled,
    notifications: auto.notifications,
  };
}

/** The slide-in create/edit sheet. `editing` null means create. */
export function AutomationFormPanel({
  editing,
  onClose,
}: {
  editing: Automation | null;
  onClose: () => void;
}) {
  const toast = useToast();

  async function handleSave(payload: CreateAutomationPayload) {
    try {
      if (editing) {
        await commitUpdateAutomation(editing.id, payload);
        toast.push('Automation updated', 'success');
      } else {
        await commitCreateAutomation(payload);
        toast.push('Automation created', 'success');
      }
      await refreshAutomationList();
      onClose();
    } catch (err) {
      toast.push((err as Error).message || 'Failed to save', 'error');
    }
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <>
      <div {...stylex.props(panel.backdrop)} onClick={onClose} />
      <div {...stylex.props(panel.root)}>
        <div {...stylex.props(panel.header)}>
          <span>{editing ? 'Edit Automation' : 'New Automation'}</span>
          <button {...stylex.props(closeBtn.base)} onClick={onClose} aria-label="Close">
            <XIcon size={14} />
          </button>
        </div>
        <div {...stylex.props(panel.body)}>
          <AutomationForm
            initialValues={editing ?? undefined}
            onSave={handleSave}
            onCancel={onClose}
          />
        </div>
      </div>
    </>
  );
}

/** A run's status as a pill — pulsing while it runs. */
export function RunStatusPill({status}: {status: string}) {
  const variant =
    status === 'done'
      ? pill.done
      : status === 'error'
        ? pill.error
        : status === 'running'
          ? pill.running
          : null;
  return (
    <span {...stylex.props(pill.base, variant)}>
      {status === 'running' ? (
        <span {...stylex.props(pill.dot)} />
      ) : (
        <span {...stylex.props(statusDot.base, dotVariant(status))} />
      )}
      {status.replace('_', ' ')}
    </span>
  );
}
