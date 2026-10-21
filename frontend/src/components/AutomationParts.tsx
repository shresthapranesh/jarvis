import * as stylex from '@stylexjs/stylex';
import {Suspense} from 'react';

import {describeCron} from '../lib/format';
import {useToast} from '../lib/toast';
import type {Automation, AutomationInputType, CreateAutomationPayload} from '../lib/types';
import {refreshAutomationList} from '../relay/AutomationListQuery';
import {commitCreateAutomation} from '../relay/CreateAutomationMutation';
import {commitUpdateAutomation} from '../relay/UpdateAutomationMutation';
import {chip, statusDot} from '../routes/automation.styles';
import {pill} from './AutomationDetail.styles';
import {AutomationEditor, AutomationEditorLoading} from './AutomationEditor';
import {BoltIcon, CalendarIcon, CodeIcon, EyeIcon, WebhookIcon} from './icons';

/* The pieces the automation list, detail and run pages share. */

export function TypeIcon({type, size = 14}: {type: AutomationInputType; size?: number}) {
  if (type === 'prompt') return <BoltIcon size={size} />;
  if (type === 'code') return <CodeIcon size={size} />;
  if (type === 'monitor') return <EyeIcon size={size} />;
  return <WebhookIcon size={size} />;
}

/**
 * When an automation runs, said the way a person would ("Weekdays at 7:00"),
 * with the cron line itself on hover. A cron the describer doesn't cover is
 * shown as written, in mono, as before.
 */
export function ScheduleChip({schedule}: {schedule: string | null | undefined}) {
  if (!schedule) return <span {...stylex.props(chip.base, chip.adhoc)}>ad-hoc</span>;
  const readable = describeCron(schedule);
  return (
    <span
      {...stylex.props(chip.base, chip.schedule, readable !== null && chip.scheduleReadable)}
      title={`cron: ${schedule}`}
    >
      <CalendarIcon size={11} />
      {readable ?? schedule}
    </span>
  );
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
    stateful: auto.stateful,
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

  // A failure rejects back into the sheet, which shows it and stays open.
  async function handleSave(payload: CreateAutomationPayload) {
    if (editing) await commitUpdateAutomation(editing.id, payload);
    else await commitCreateAutomation(payload);
    toast.push(editing ? 'Automation updated' : 'Automation created', 'success');
    await refreshAutomationList();
    onClose();
  }

  // The editor suspends on the model catalog the first time; the empty sheet
  // stands in so the page behind it doesn't drop to its own fallback.
  return (
    <Suspense fallback={<AutomationEditorLoading onClose={onClose} />}>
      <AutomationEditor editing={editing} onSave={handleSave} onClose={onClose} />
    </Suspense>
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
