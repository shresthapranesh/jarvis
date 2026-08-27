import * as stylex from '@stylexjs/stylex';
import {marked} from 'marked';
import {useEffect, useMemo, useRef, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {AutomationRunsQuery as TAutomationRunsQuery} from '../__generated__/AutomationRunsQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {useAutomationRunEvents} from '../hooks/useAutomationRunEvents';
import {formatDuration, formatRelativeTime} from '../lib/api';
import {useToast} from '../lib/toast';
import type {Automation, AutomationRun} from '../lib/types';
import {refreshAutomationList} from '../relay/AutomationListQuery';
import {
  automationRunsQuery,
  automationRunsVars,
  mapAutomationRun,
  refreshAutomationRuns,
} from '../relay/AutomationRunsQuery';
import {commitTriggerAutomation} from '../relay/TriggerAutomationMutation';
import {panel, statusDot, typeIcon} from '../routes/automation.styles';
import {headBtn, live, sheet, timeline} from './AutomationRunsPanel.styles';
import {
  BoltIcon,
  CalendarIcon,
  ChevronDownIcon,
  CodeIcon,
  CursorClickIcon,
  EditIcon,
  EyeIcon,
  PlayIcon,
  WebhookIcon,
  XIcon,
} from './icons';
import {QueryBoundary, useQueryRetry} from './QueryBoundary';
import {errorBubble, prose, stream, ThinkingDots} from './ui';

function TypeIcon({type, size = 16}: {type: Automation['input_type']; size?: number}) {
  if (type === 'prompt') return <BoltIcon size={size} />;
  if (type === 'code') return <CodeIcon size={size} />;
  if (type === 'monitor') return <EyeIcon size={size} />;
  return <WebhookIcon size={size} />;
}

// ── Live run card ─────────────────────────────────────────────────────────────

function useElapsedSeconds(active: boolean): number {
  const [secs, setSecs] = useState(0);
  const startRef = useRef<number | null>(null);

  useEffect(() => {
    if (!active) {
      startRef.current = null;
      return;
    }
    startRef.current = Date.now();
    setSecs(0);
    const id = setInterval(() => {
      if (startRef.current) {
        setSecs(Math.floor((Date.now() - startRef.current) / 1000));
      }
    }, 1000);
    return () => clearInterval(id);
  }, [active]);

  return secs;
}

const PILL = {
  running: live.pillRunning,
  done: live.pillDone,
  error: live.pillError,
} as const;

function LiveRunCard({
  runId,
  automationId,
  onComplete,
}: {
  runId: string;
  automationId: string;
  onComplete: () => void;
}) {
  const {streaming, text, error} = useAutomationRunEvents(runId, automationId);
  const elapsed = useElapsedSeconds(streaming);
  const scrollRef = useRef<HTMLDivElement>(null);
  const stickToBottomRef = useRef(true);
  const [completed, setCompleted] = useState(false);

  useEffect(() => {
    if (!streaming && !completed) {
      setCompleted(true);
      const t = setTimeout(onComplete, 350);
      return () => clearTimeout(t);
    }
  }, [streaming, completed, onComplete]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    if (stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [text]);

  function onScroll(e: React.UIEvent<HTMLDivElement>) {
    const el = e.currentTarget;
    stickToBottomRef.current = el.scrollHeight - el.clientHeight - el.scrollTop < 40;
  }

  const status: 'running' | 'done' | 'error' = error ? 'error' : streaming ? 'running' : 'done';

  const fmtElapsed = elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`;

  return (
    <div
      {...stylex.props(
        live.card,
        status === 'running' && live.cardRunning,
        completed && live.cardFading,
      )}
    >
      <div {...stylex.props(live.header)}>
        <span {...stylex.props(live.pill, PILL[status])}>
          {status === 'running' && <span {...stylex.props(live.dot)} />}
          {status === 'running' ? 'Running' : status === 'done' ? 'Done' : 'Error'}
        </span>
        <span {...stylex.props(live.timer)}>{fmtElapsed}</span>
      </div>
      <div {...stylex.props(live.body)} ref={scrollRef} onScroll={onScroll}>
        {error ? (
          <div {...stylex.props(errorBubble.base)}>{error}</div>
        ) : text ? (
          <div
            {...stylex.props(prose.vars)}
            data-md
            dangerouslySetInnerHTML={{__html: marked.parse(text) as string}}
          />
        ) : (
          <div {...stylex.props(live.thinking)}>
            <ThinkingDots />
          </div>
        )}
        {streaming && text && <span {...stylex.props(stream.cursor)} />}
      </div>
    </div>
  );
}

// ── Run timeline row ──────────────────────────────────────────────────────────

function snippetFromText(text: string, maxChars = 240): string {
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

// Run status is server data, so the variant is looked up rather than encoded
// as a fixed set of props — an unknown status falls back to the base style.
function statusVariant(status: string) {
  if (status === 'done') return timeline.statusDone;
  if (status === 'error') return timeline.statusError;
  if (status === 'running') return timeline.statusRunning;
  return null;
}

function barVariant(status: string) {
  if (status === 'done') return timeline.barDone;
  if (status === 'error') return timeline.barError;
  if (status === 'running') return timeline.barRunning;
  return null;
}

function dotVariant(status: string) {
  if (status === 'done') return statusDot.done;
  if (status === 'error') return statusDot.error;
  if (status === 'running') return statusDot.running;
  if (status === 'no_change') return statusDot.no_change;
  if (status === 'skipped') return statusDot.skipped;
  if (status === 'stopped') return statusDot.stopped;
  if (status === 'blocked') return statusDot.blocked;
  return null;
}

function RunTimelineRow({run, maxDurationMs}: {run: AutomationRun; maxDurationMs: number}) {
  const [open, setOpen] = useState(false);
  const text = run.output ?? run.error ?? '';
  const snippet = text ? snippetFromText(text) : '';
  const duration = formatDuration(run.started_at, run.finished_at);
  const durationMs = run.finished_at
    ? new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()
    : 0;
  const barPct = maxDurationMs > 0 ? Math.max(8, (durationMs / maxDurationMs) * 100) : 8;

  return (
    <div {...stylex.props(timeline.row, open && timeline.rowOpen)}>
      <div
        {...stylex.props(timeline.head)}
        onClick={() => text && setOpen((o) => !o)}
        role="button"
        tabIndex={0}
      >
        <div {...stylex.props(timeline.meta)}>
          <span {...stylex.props(statusDot.base, dotVariant(run.status))} />
          <span {...stylex.props(timeline.status, statusVariant(run.status))}>{run.status}</span>
          <span {...stylex.props(timeline.trigger)} title={run.triggered_by}>
            {run.triggered_by === 'schedule' ? (
              <CalendarIcon size={11} />
            ) : (
              <CursorClickIcon size={11} />
            )}
          </span>
          <span {...stylex.props(timeline.time)}>{formatRelativeTime(run.started_at)}</span>
          {duration && (
            <span {...stylex.props(timeline.duration)}>
              <span
                {...stylex.props(timeline.bar, barVariant(run.status))}
                style={{width: `${barPct}%`}}
              />
              <span {...stylex.props(timeline.barLabel)}>{duration}</span>
            </span>
          )}
          {text && (
            <span {...stylex.props(timeline.chevron, open && timeline.chevronOpen)}>
              <ChevronDownIcon size={12} />
            </span>
          )}
        </div>
        {snippet && !open && (
          <div {...stylex.props(timeline.snippet, run.status === 'error' && timeline.snippetError)}>
            {snippet}
          </div>
        )}
      </div>
      {open && text && (
        <div
          {...stylex.props(
            timeline.output,
            prose.vars,
            run.status === 'error' && timeline.outputError,
          )}
          data-md
          dangerouslySetInnerHTML={{__html: marked.parse(text) as string}}
        />
      )}
    </div>
  );
}

// ── Runs panel (slide-in from right) ──────────────────────────────────────────

interface PanelProps {
  automation: Automation;
  onClose: () => void;
  onEdit: (auto: Automation) => void;
}

export function AutomationRunsPanel(props: PanelProps) {
  return (
    <QueryBoundary
      label="Failed to load runs"
      fallback={<div {...stylex.props(sheet.empty)}>Loading runs…</div>}
    >
      <AutomationRunsPanelInner {...props} />
    </QueryBoundary>
  );
}

function AutomationRunsPanelInner({automation, onClose, onEdit}: PanelProps) {
  const toast = useToast();
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const data = useLazyLoadQuery<TAutomationRunsQuery>(
    automationRunsQuery,
    automationRunsVars(automation.id),
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const runs = useMemo(() => data.automationRuns.map(mapAutomationRun), [data.automationRuns]);

  const triggerAction = useAsyncAction(
    async () => {
      const {run_id} = await commitTriggerAutomation(automation.id);
      setActiveRunId(run_id);
      toast.push(`Started "${automation.name}"`, 'info');
    },
    {onError: (err) => toast.push(err.message || 'Failed to trigger', 'error')},
  );

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  const maxDurationMs = runs.reduce((max, r) => {
    if (!r.finished_at) return max;
    const ms = new Date(r.finished_at).getTime() - new Date(r.started_at).getTime();
    return ms > max ? ms : max;
  }, 0);

  return (
    <>
      <div {...stylex.props(panel.backdrop)} onClick={onClose} />
      <aside {...stylex.props(sheet.root)} role="dialog" aria-label={`Runs for ${automation.name}`}>
        <header {...stylex.props(sheet.header)}>
          <div {...stylex.props(sheet.title)}>
            <span
              {...stylex.props(typeIcon.base, sheet.typeIconSm, typeIcon[automation.input_type])}
            >
              <TypeIcon type={automation.input_type} size={14} />
            </span>
            <span {...stylex.props(sheet.name)} title={automation.name}>
              {automation.name}
            </span>
          </div>
          <div {...stylex.props(sheet.actions)}>
            <button
              {...stylex.props(headBtn.base, headBtn.primary)}
              onClick={() => void triggerAction.run()}
              disabled={triggerAction.pending || !!activeRunId}
              title="Run now"
            >
              <PlayIcon size={12} />
              <span>Run</span>
            </button>
            <button {...stylex.props(headBtn.base)} onClick={() => onEdit(automation)} title="Edit">
              <EditIcon size={13} />
            </button>
            <button
              {...stylex.props(headBtn.base, headBtn.close)}
              onClick={onClose}
              aria-label="Close"
            >
              <XIcon size={14} />
            </button>
          </div>
        </header>

        <div {...stylex.props(sheet.body)}>
          {activeRunId && (
            <LiveRunCard
              runId={activeRunId}
              automationId={automation.id}
              onComplete={() => {
                setActiveRunId(null);
                // A finished run changes both this history and the list card's
                // last_run_status.
                void refreshAutomationRuns(automation.id);
                void refreshAutomationList();
              }}
            />
          )}

          <div {...stylex.props(sheet.sectionLabel)}>
            History {runs.length > 0 && <span {...stylex.props(sheet.count)}>({runs.length})</span>}
          </div>

          {runs.length === 0 && !activeRunId && (
            <div {...stylex.props(sheet.empty)}>
              <BoltIcon size={20} />
              <p {...stylex.props(sheet.emptyP)}>No runs yet. Hit Run to fire one.</p>
            </div>
          )}

          {runs.length > 0 && (
            <div {...stylex.props(timeline.list)}>
              {runs.map((r) => (
                <RunTimelineRow key={r.id} run={r} maxDurationMs={maxDurationMs} />
              ))}
            </div>
          )}
        </div>
      </aside>
    </>
  );
}
