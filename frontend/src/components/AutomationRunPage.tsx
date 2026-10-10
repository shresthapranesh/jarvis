import * as stylex from '@stylexjs/stylex';
import {Link, useParams} from '@tanstack/react-router';
import {useEffect, useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {AutomationListQuery as TAutomationListQuery} from '../__generated__/AutomationListQuery.graphql';
import type {AutomationRunsQuery as TAutomationRunsQuery} from '../__generated__/AutomationRunsQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {useAutomationRunEvents} from '../hooks/useAutomationRunEvents';
import {usePollingRefresh} from '../hooks/usePollingRefresh';
import {formatDuration, formatRelativeTime} from '../lib/api';
import {useToast} from '../lib/toast';
import type {AutomationRun} from '../lib/types';
import {automationListQuery} from '../relay/AutomationListQuery';
import {
  automationRunsQuery,
  automationRunsVars,
  mapAutomationRun,
  refreshAutomationRuns,
} from '../relay/AutomationRunsQuery';
import {decodeGlobalId} from '../relay/globalId';
import {commitStopAutomationRun} from '../relay/StopAutomationRunMutation';
import {detail, run as runStyles} from './AutomationDetail.styles';
import {RunStatusPill} from './AutomationParts';
import {CalendarIcon, ChevronLeftIcon, CursorClickIcon, StopIcon} from './icons';
import {Markdown, StreamingMarkdown} from './Markdown';
import {QueryBoundary, useQueryRetry} from './QueryBoundary';
import {btn, errorBubble, page, prose, stream, ThinkingDots} from './ui';

export default function AutomationRunPage() {
  return (
    <QueryBoundary
      label="Failed to load run"
      fallback={<div {...stylex.props(detail.empty)}>Loading run…</div>}
    >
      <AutomationRunView />
    </QueryBoundary>
  );
}

function AutomationRunView() {
  const {id, runId} = useParams({from: '/automation/$id/runs/$runId'});
  const fetchKey = useQueryRetry();

  const listData = useLazyLoadQuery<TAutomationListQuery>(
    automationListQuery,
    {},
    {fetchPolicy: 'store-or-network', fetchKey},
  );
  const name = listData.automations.find((a) => decodeGlobalId(a.id) === id)?.name ?? 'Automation';

  const runsData = useLazyLoadQuery<TAutomationRunsQuery>(
    automationRunsQuery,
    automationRunsVars(id),
    {fetchPolicy: 'store-and-network', fetchKey},
  );
  const runs = useMemo(() => runsData.automationRuns.map(mapAutomationRun), [runsData]);
  const index = runs.findIndex((r) => r.id === runId);
  const run = index >= 0 ? runs[index] : null;
  // Runs are listed newest first.
  const newer = index > 0 ? runs[index - 1] : null;
  const older = index >= 0 && index < runs.length - 1 ? runs[index + 1] : null;

  // The live stream refetches on its terminal event; this covers a stream
  // that never connects (e.g. the server restarted mid-run).
  usePollingRefresh(() => refreshAutomationRuns(id), run?.status === 'running' ? 5_000 : null);

  return (
    <div {...stylex.props(page.root)}>
      <header {...stylex.props(detail.header)}>
        <Link to="/automation/$id" params={{id}} {...stylex.props(detail.back)}>
          <ChevronLeftIcon size={13} />
          {name}
        </Link>

        <div {...stylex.props(detail.titleRow)}>
          <div {...stylex.props(detail.titleBlock)}>
            <h1 {...stylex.props(detail.title)}>
              {run ? `Run · ${new Date(run.started_at).toLocaleString()}` : 'Run'}
            </h1>
            {run && <RunFacts run={run} />}
          </div>

          <div {...stylex.props(detail.actions)}>
            {run?.status === 'running' && <StopButton runId={run.id} automationId={id} />}
            <div {...stylex.props(runStyles.nav)}>
              <RunNavLink id={id} target={newer} label="Newer" />
              <RunNavLink id={id} target={older} label="Older" />
            </div>
          </div>
        </div>
      </header>

      <div {...stylex.props(detail.body)}>
        {!run ? (
          <div {...stylex.props(detail.empty)}>This run doesn't exist any more.</div>
        ) : run.status === 'running' ? (
          <LiveOutput runId={run.id} automationId={id} />
        ) : (
          <StoredOutput run={run} />
        )}
      </div>
    </div>
  );
}

function RunFacts({run}: {run: AutomationRun}) {
  const duration = formatDuration(run.started_at, run.finished_at);
  return (
    <div {...stylex.props(runStyles.facts)}>
      <RunStatusPill status={run.status} />
      <span {...stylex.props(detail.meta)}>
        {run.triggered_by === 'schedule' ? (
          <CalendarIcon size={11} />
        ) : (
          <CursorClickIcon size={11} />
        )}
        {run.triggered_by === 'schedule' ? 'Scheduled' : 'Manual'}
      </span>
      <span {...stylex.props(detail.meta)}>Started {formatRelativeTime(run.started_at)}</span>
      {run.status === 'running' ? (
        <Elapsed since={run.started_at} />
      ) : (
        duration && <span {...stylex.props(detail.meta)}>Took {duration}</span>
      )}
    </div>
  );
}

function Elapsed({since}: {since: string}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const secs = Math.max(0, Math.floor((now - new Date(since).getTime()) / 1000));
  const label = secs < 60 ? `${secs}s` : `${Math.floor(secs / 60)}m ${secs % 60}s`;
  return <span {...stylex.props(detail.meta, runStyles.timer)}>{label}</span>;
}

function RunNavLink({
  id,
  target,
  label,
}: {
  id: string;
  target: AutomationRun | null;
  label: string;
}) {
  if (!target) {
    return (
      <button {...stylex.props(btn.base, btn.small)} disabled>
        {label}
      </button>
    );
  }
  return (
    <Link
      to="/automation/$id/runs/$runId"
      params={{id, runId: target.id}}
      {...stylex.props(btn.base, btn.small)}
    >
      {label}
    </Link>
  );
}

function StopButton({runId, automationId}: {runId: string; automationId: string}) {
  const toast = useToast();
  const stop = useAsyncAction(
    async () => {
      await commitStopAutomationRun(runId);
      await refreshAutomationRuns(automationId);
    },
    {onError: (err) => toast.push(err.message || 'Failed to stop', 'error')},
  );
  return (
    <button
      {...stylex.props(btn.base, btn.danger)}
      disabled={stop.pending}
      onClick={() => void stop.run()}
    >
      <StopIcon size={12} />
      Stop
    </button>
  );
}

function LiveOutput({runId, automationId}: {runId: string; automationId: string}) {
  const {streaming, text, error} = useAutomationRunEvents(runId, automationId);
  if (error) return <div {...stylex.props(errorBubble.base, runStyles.output)}>{error}</div>;
  if (!text) {
    return (
      <div {...stylex.props(runStyles.thinking)}>
        <ThinkingDots />
      </div>
    );
  }
  return (
    <div {...stylex.props(runStyles.output)}>
      <div {...stylex.props(prose.vars)} data-md>
        <StreamingMarkdown text={text} />
      </div>
      {streaming && <span {...stylex.props(stream.cursor)} />}
    </div>
  );
}

function StoredOutput({run}: {run: AutomationRun}) {
  if (!run.output && !run.error) {
    return (
      <div {...stylex.props(detail.empty)}>
        {run.status === 'no_change'
          ? 'No change since the last run.'
          : 'This run produced no output.'}
      </div>
    );
  }
  return (
    <>
      {run.error && <div {...stylex.props(errorBubble.base, runStyles.output)}>{run.error}</div>}
      {run.output && (
        <div {...stylex.props(runStyles.output, prose.vars)} data-md>
          <Markdown text={run.output} />
        </div>
      )}
    </>
  );
}
