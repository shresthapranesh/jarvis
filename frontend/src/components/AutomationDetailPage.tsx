import * as stylex from '@stylexjs/stylex';
import {Link, useNavigate, useParams} from '@tanstack/react-router';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {AutomationListQuery as TAutomationListQuery} from '../__generated__/AutomationListQuery.graphql';
import type {AutomationRunsQuery as TAutomationRunsQuery} from '../__generated__/AutomationRunsQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {usePollingRefresh} from '../hooks/usePollingRefresh';
import {formatDuration, formatNextRun, formatRelativeTime} from '../lib/api';
import {useToast} from '../lib/toast';
import type {Automation, AutomationRun} from '../lib/types';
import {
  automationListQuery,
  mapAutomation,
  refreshAutomationList,
} from '../relay/AutomationListQuery';
import {
  automationRunsQuery,
  automationRunsVars,
  mapAutomationRun,
  refreshAutomationRuns,
} from '../relay/AutomationRunsQuery';
import {commitDeleteAutomation} from '../relay/DeleteAutomationMutation';
import {decodeGlobalId} from '../relay/globalId';
import {commitTriggerAutomation} from '../relay/TriggerAutomationMutation';
import {commitUpdateAutomation} from '../relay/UpdateAutomationMutation';
import {card, chip, confirmWarn, typeIcon} from '../routes/automation.styles';
import {bar, detail, runRow, toggle} from './AutomationDetail.styles';
import {AutomationFormPanel, RunStatusPill, toggledPayload, TypeIcon} from './AutomationParts';
import {ConfirmDialog} from './ConfirmDialog';
import {
  BoltIcon,
  CalendarIcon,
  ChevronDownIcon,
  ChevronLeftIcon,
  ClockIcon,
  CursorClickIcon,
  EditIcon,
  PlayIcon,
  TrashIcon,
} from './icons';
import {QueryBoundary, useQueryRetry} from './QueryBoundary';
import {btn, page} from './ui';

export default function AutomationDetailPage() {
  return (
    <QueryBoundary
      label="Failed to load automation"
      fallback={<div {...stylex.props(detail.empty)}>Loading…</div>}
    >
      <AutomationDetail />
    </QueryBoundary>
  );
}

function AutomationDetail() {
  const {id} = useParams({from: '/automation/$id/'});
  const navigate = useNavigate();
  const toast = useToast();
  const fetchKey = useQueryRetry();

  // The list query, not a single-automation one: it is the one the list page
  // and its 30s poll keep warm, so coming from the list renders immediately.
  const listData = useLazyLoadQuery<TAutomationListQuery>(
    automationListQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey},
  );
  const node = listData.automations.find((a) => decodeGlobalId(a.id) === id);
  const auto = useMemo(() => (node ? mapAutomation(node) : null), [node]);

  const runsData = useLazyLoadQuery<TAutomationRunsQuery>(
    automationRunsQuery,
    automationRunsVars(id),
    {fetchPolicy: 'store-and-network', fetchKey},
  );
  const runs = useMemo(() => runsData.automationRuns.map(mapAutomationRun), [runsData]);
  const anyRunning = runs.some((r) => r.status === 'running');

  // Scheduled runs start without anyone touching the page; a running one
  // finishes on its own.
  usePollingRefresh(() => refreshAutomationRuns(id), anyRunning ? 4_000 : 30_000);
  usePollingRefresh(refreshAutomationList, 30_000);

  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [configOpen, setConfigOpen] = useState(false);

  const triggerAction = useAsyncAction(
    async (a: Automation) => {
      const {run_id} = await commitTriggerAutomation(a.id);
      toast.push(`Started "${a.name}"`, 'info');
      await refreshAutomationRuns(a.id);
      void navigate({to: '/automation/$id/runs/$runId', params: {id: a.id, runId: run_id}});
    },
    {onError: (err) => toast.push(err.message || 'Failed to trigger', 'error')},
  );

  const toggleAction = useAsyncAction(
    async (a: Automation) => {
      await commitUpdateAutomation(a.id, toggledPayload(a));
      await refreshAutomationList();
      toast.push(a.enabled ? 'Automation paused' : 'Automation enabled', 'success');
    },
    {onError: (err) => toast.push(err.message || 'Failed to update', 'error')},
  );

  const deleteAction = useAsyncAction(
    async (a: Automation) => {
      await commitDeleteAutomation(a.id);
      await refreshAutomationList();
      toast.push(`Deleted "${a.name}"`, 'success');
      void navigate({to: '/automation'});
    },
    {onError: (err) => toast.push(err.message || 'Failed to delete', 'error')},
  );

  if (!auto) {
    return (
      <div {...stylex.props(detail.body)}>
        <Link to="/automation" {...stylex.props(detail.back)}>
          <ChevronLeftIcon size={13} />
          Automations
        </Link>
        <div {...stylex.props(detail.empty)}>This automation doesn't exist any more.</div>
      </div>
    );
  }

  const maxDurationMs = runs.reduce((max, r) => Math.max(max, runDurationMs(r)), 0);
  const config = configText(auto);

  return (
    <div {...stylex.props(page.root)}>
      <header {...stylex.props(detail.header)}>
        <Link to="/automation" {...stylex.props(detail.back)}>
          <ChevronLeftIcon size={13} />
          Automations
        </Link>

        <div {...stylex.props(detail.titleRow)}>
          <div {...stylex.props(detail.identity)}>
            <div {...stylex.props(typeIcon.base, typeIcon[auto.input_type])}>
              <TypeIcon type={auto.input_type} size={16} />
            </div>
            <div {...stylex.props(detail.titleBlock)}>
              <div {...stylex.props(detail.titleLine)}>
                <h1 {...stylex.props(detail.title)} title={auto.name}>
                  {auto.name}
                </h1>
                {!auto.enabled && <span {...stylex.props(card.pausedPill)}>Paused</span>}
              </div>
              {auto.description && <p {...stylex.props(detail.desc)}>{auto.description}</p>}
              <div {...stylex.props(detail.badges)}>
                <span {...stylex.props(chip.base, chip[auto.input_type])}>{auto.input_type}</span>
                {auto.schedule ? (
                  <span
                    {...stylex.props(chip.base, chip.schedule)}
                    title={`cron: ${auto.schedule}`}
                  >
                    <CalendarIcon size={11} />
                    {auto.schedule}
                  </span>
                ) : (
                  <span {...stylex.props(chip.base, chip.adhoc)}>ad-hoc</span>
                )}
                {auto.next_run_at && auto.enabled && (
                  <span {...stylex.props(detail.meta, detail.metaAccent)}>
                    <ClockIcon size={11} />
                    Next {formatNextRun(auto.next_run_at)}
                  </span>
                )}
                {(auto.total_count_7d ?? 0) > 0 && (
                  <span {...stylex.props(detail.meta)}>
                    {auto.success_count_7d ?? 0}/{auto.total_count_7d} ok · 7d
                  </span>
                )}
                {auto.stateful && auto.conversation_id && (
                  <Link
                    to="/c/$id"
                    params={{id: auto.conversation_id}}
                    {...stylex.props(detail.meta, detail.metaLink)}
                  >
                    Open thread
                  </Link>
                )}
              </div>
            </div>
          </div>

          <div {...stylex.props(detail.actions)}>
            <button
              {...stylex.props(btn.base)}
              title={auto.enabled ? 'Pause schedule' : 'Resume schedule'}
              aria-pressed={auto.enabled}
              disabled={toggleAction.pending}
              onClick={() => void toggleAction.run(auto)}
            >
              <span {...stylex.props(toggle.track, auto.enabled ? toggle.on : toggle.off)}>
                <span {...stylex.props(toggle.dot)} />
              </span>
              {auto.enabled ? 'Enabled' : 'Paused'}
            </button>
            <button
              {...stylex.props(btn.base, btn.primary)}
              disabled={triggerAction.pending}
              onClick={() => void triggerAction.run(auto)}
            >
              <PlayIcon size={12} />
              Run now
            </button>
            <button {...stylex.props(btn.base)} title="Edit" onClick={() => setEditing(true)}>
              <EditIcon size={13} />
            </button>
            <button
              {...stylex.props(btn.base, btn.danger)}
              title="Delete"
              onClick={() => setConfirmDelete(true)}
            >
              <TrashIcon size={13} />
            </button>
          </div>
        </div>
      </header>

      <div {...stylex.props(detail.body)}>
        {config && (
          <section {...stylex.props(detail.section)}>
            <div {...stylex.props(detail.sectionLabel)}>
              <button
                {...stylex.props(detail.sectionToggle)}
                aria-expanded={configOpen}
                onClick={() => setConfigOpen((o) => !o)}
              >
                <span {...stylex.props(detail.chevron, configOpen && detail.chevronOpen)}>
                  <ChevronDownIcon size={11} />
                </span>
                {config.label}
              </button>
            </div>
            {configOpen && <pre {...stylex.props(detail.config)}>{config.text}</pre>}
          </section>
        )}

        <section {...stylex.props(detail.section)}>
          <div {...stylex.props(detail.sectionLabel)}>
            Runs {runs.length > 0 && <span {...stylex.props(detail.count)}>{runs.length}</span>}
          </div>

          {runs.length === 0 ? (
            <div {...stylex.props(detail.empty)}>
              <BoltIcon size={20} />
              <p {...stylex.props(detail.emptyP)}>No runs yet. Hit Run now to fire one.</p>
            </div>
          ) : (
            <div {...stylex.props(runRow.list)}>
              {runs.map((r) => (
                <RunRow key={r.id} run={r} maxDurationMs={maxDurationMs} />
              ))}
            </div>
          )}
        </section>
      </div>

      {editing && <AutomationFormPanel editing={auto} onClose={() => setEditing(false)} />}

      <ConfirmDialog
        open={confirmDelete}
        title="Delete automation?"
        danger
        confirmLabel="Delete"
        requireTypedName={auto.name}
        message={
          <>
            <p>
              This permanently deletes <strong>{auto.name}</strong> and all of its run history
              {(auto.total_count_7d ?? 0) > 0 && (
                <> ({auto.total_count_7d} runs in the last 7 days)</>
              )}
              .
            </p>
            <p {...stylex.props(confirmWarn.base)}>This cannot be undone.</p>
          </>
        }
        onConfirm={() => void deleteAction.run(auto)}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}

// ── Run row ───────────────────────────────────────────────────────────────────

function RunRow({run, maxDurationMs}: {run: AutomationRun; maxDurationMs: number}) {
  const text = run.error ?? run.output ?? '';
  const duration = formatDuration(run.started_at, run.finished_at);
  const ms = runDurationMs(run);
  const barPct = maxDurationMs > 0 ? Math.max(8, (ms / maxDurationMs) * 100) : 8;

  return (
    <Link
      to="/automation/$id/runs/$runId"
      params={{id: run.automation_id, runId: run.id}}
      {...stylex.props(runRow.root)}
    >
      <span {...stylex.props(runRow.status)}>
        <RunStatusPill status={run.status} />
      </span>
      <span {...stylex.props(runRow.trigger)} title={`Triggered by ${run.triggered_by}`}>
        {run.triggered_by === 'schedule' ? (
          <CalendarIcon size={12} />
        ) : (
          <CursorClickIcon size={12} />
        )}
      </span>
      <span {...stylex.props(runRow.time)} title={new Date(run.started_at).toLocaleString()}>
        {formatRelativeTime(run.started_at)}
      </span>
      <span {...stylex.props(runRow.snippet, run.status === 'error' && runRow.snippetError)}>
        {text ? snippetFromText(text) : ''}
      </span>
      <span {...stylex.props(runRow.duration)}>
        {duration && (
          <>
            <span
              {...stylex.props(bar.base, barVariant(run.status))}
              style={{width: `${barPct}%`}}
            />
            <span {...stylex.props(bar.label)}>{duration}</span>
          </>
        )}
      </span>
      <span {...stylex.props(runRow.arrow)}>
        <ChevronLeftIcon size={12} />
      </span>
    </Link>
  );
}

function barVariant(status: string) {
  if (status === 'done') return bar.done;
  if (status === 'error') return bar.error;
  if (status === 'running') return bar.running;
  return null;
}

function runDurationMs(run: AutomationRun): number {
  if (!run.finished_at) return 0;
  return new Date(run.finished_at).getTime() - new Date(run.started_at).getTime();
}

function snippetFromText(text: string, maxChars = 200): string {
  const stripped = text
    .replace(/```[\s\S]*?```/g, '[code]')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/[#>*_~]+/g, '')
    .replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
    .replace(/\s+/g, ' ')
    .trim();
  if (stripped.length <= maxChars) return stripped;
  return stripped.slice(0, maxChars).trimEnd() + '…';
}

/** What the automation actually does, for the collapsible config block. */
function configText(auto: Automation): {label: string; text: string} | null {
  if (auto.input_type === 'webhook') {
    if (!auto.webhook_url) return null;
    const lines = [`${auto.webhook_method ?? 'POST'} ${auto.webhook_url}`];
    if (auto.webhook_headers) lines.push('', auto.webhook_headers);
    if (auto.webhook_body) lines.push('', auto.webhook_body);
    return {label: 'Request', text: lines.join('\n')};
  }
  if (auto.input_type === 'code') {
    return auto.code_text ? {label: 'Code', text: auto.code_text} : null;
  }
  if (!auto.prompt_text) return null;
  return {
    label: auto.model ? `Prompt · ${auto.model}` : 'Prompt',
    text: auto.prompt_text,
  };
}
