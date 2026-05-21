import {useQuery, useMutation, useQueryClient} from '@tanstack/react-query';
import {marked} from 'marked';
import {useEffect, useRef, useState} from 'react';

import {useAutomationRunEvents} from '../hooks/useAutomationRunEvents';
import {formatDuration, formatRelativeTime} from '../lib/api';
import {fetchAutomationRuns} from '../relay/AutomationRunsQuery';
import {commitTriggerAutomation} from '../relay/TriggerAutomationMutation';
import {useToast} from '../lib/toast';
import type {Automation, AutomationRun} from '../lib/types';
import {
  BoltIcon,
  CalendarIcon,
  ChevronDownIcon,
  CodeIcon,
  CursorClickIcon,
  EditIcon,
  PlayIcon,
  WebhookIcon,
  XIcon,
} from './icons';

function TypeIcon({type, size = 16}: {type: Automation['input_type']; size?: number}) {
  if (type === 'prompt') return <BoltIcon size={size} />;
  if (type === 'code') return <CodeIcon size={size} />;
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
    stickToBottomRef.current =
      el.scrollHeight - el.clientHeight - el.scrollTop < 40;
  }

  const status: 'running' | 'done' | 'error' = error
    ? 'error'
    : streaming
    ? 'running'
    : 'done';

  const fmtElapsed =
    elapsed < 60 ? `${elapsed}s` : `${Math.floor(elapsed / 60)}m ${elapsed % 60}s`;

  return (
    <div
      className={`live-run-card live-run-card--${status}${completed ? ' live-run-card--fading' : ''}`}
    >
      <div className="live-run-header">
        <span className={`live-run-pill live-run-pill--${status}`}>
          {status === 'running' && <span className="live-run-dot" />}
          {status === 'running' ? 'Running' : status === 'done' ? 'Done' : 'Error'}
        </span>
        <span className="live-run-timer">{fmtElapsed}</span>
      </div>
      <div className="live-run-body" ref={scrollRef} onScroll={onScroll}>
        {error ? (
          <div className="error-bubble">{error}</div>
        ) : text ? (
          <div
            className="live-run-output"
            dangerouslySetInnerHTML={{__html: marked.parse(text) as string}}
          />
        ) : (
          <div className="live-run-thinking">
            <div className="thinking-dots">
              <span />
              <span />
              <span />
            </div>
          </div>
        )}
        {streaming && text && <span className="cursor" />}
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
    <div className={`run-row-v2 run-row-v2--${run.status}${open ? ' run-row-v2--open' : ''}`}>
      <div
        className="run-row-v2-header"
        onClick={() => text && setOpen((o) => !o)}
        role="button"
        tabIndex={0}
      >
        <div className="run-row-v2-meta">
          <span className={`run-status-dot run-status-dot--${run.status}`} />
          <span className={`run-row-v2-status run-row-v2-status--${run.status}`}>
            {run.status}
          </span>
          <span className="run-trigger-icon" title={run.triggered_by}>
            {run.triggered_by === 'schedule' ? (
              <CalendarIcon size={11} />
            ) : (
              <CursorClickIcon size={11} />
            )}
          </span>
          <span className="run-row-v2-time">{formatRelativeTime(run.started_at)}</span>
          {duration && (
            <span className="run-row-v2-duration">
              <span
                className={`run-duration-bar run-duration-bar--${run.status}`}
                style={{width: `${barPct}%`}}
              />
              <span className="run-duration-label">{duration}</span>
            </span>
          )}
          {text && (
            <span className={`run-row-v2-chevron${open ? ' run-row-v2-chevron--open' : ''}`}>
              <ChevronDownIcon size={12} />
            </span>
          )}
        </div>
        {snippet && !open && (
          <div
            className={`run-row-v2-snippet${run.status === 'error' ? ' run-row-v2-snippet--error' : ''}`}
          >
            {snippet}
          </div>
        )}
      </div>
      {open && text && (
        <div
          className={`run-row-v2-output${run.status === 'error' ? ' run-row-v2-output--error' : ''}`}
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

export function AutomationRunsPanel({automation, onClose, onEdit}: PanelProps) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [activeRunId, setActiveRunId] = useState<string | null>(null);

  const {data: runs = [], isLoading} = useQuery({
    queryKey: ['automation-runs', automation.id],
    queryFn: () => fetchAutomationRuns(automation.id),
    staleTime: 10_000,
  });

  const triggerMutation = useMutation({
    mutationFn: () => commitTriggerAutomation(automation.id),
    onSuccess: ({run_id}) => {
      setActiveRunId(run_id);
      toast.push(`Started "${automation.name}"`, 'info');
    },
    onError: (err: Error) => toast.push(err.message || 'Failed to trigger', 'error'),
  });

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
      <div className="auto-panel-backdrop" onClick={onClose} />
      <aside className="runs-panel" role="dialog" aria-label={`Runs for ${automation.name}`}>
        <header className="runs-panel-header">
          <div className="runs-panel-title">
            <span className={`runs-panel-type runs-panel-type--${automation.input_type}`}>
              <TypeIcon type={automation.input_type} size={14} />
            </span>
            <span className="runs-panel-name" title={automation.name}>
              {automation.name}
            </span>
          </div>
          <div className="runs-panel-actions">
            <button
              className="runs-panel-btn runs-panel-btn--primary"
              onClick={() => triggerMutation.mutate()}
              disabled={triggerMutation.isPending || !!activeRunId}
              title="Run now"
            >
              <PlayIcon size={12} />
              <span>Run</span>
            </button>
            <button
              className="runs-panel-btn"
              onClick={() => onEdit(automation)}
              title="Edit"
            >
              <EditIcon size={13} />
            </button>
            <button
              className="runs-panel-btn runs-panel-btn--close"
              onClick={onClose}
              aria-label="Close"
            >
              <XIcon size={14} />
            </button>
          </div>
        </header>

        <div className="runs-panel-body">
          {activeRunId && (
            <LiveRunCard
              runId={activeRunId}
              automationId={automation.id}
              onComplete={() => {
                setActiveRunId(null);
                queryClient.invalidateQueries({
                  queryKey: ['automation-runs', automation.id],
                });
                queryClient.invalidateQueries({queryKey: ['automations']});
              }}
            />
          )}

          <div className="runs-panel-section-label">
            History {runs.length > 0 && <span className="runs-count">({runs.length})</span>}
          </div>

          {isLoading && <div className="runs-empty">Loading runs…</div>}
          {!isLoading && runs.length === 0 && !activeRunId && (
            <div className="runs-empty">
              <BoltIcon size={20} />
              <p>No runs yet. Hit Run to fire one.</p>
            </div>
          )}

          {runs.length > 0 && (
            <div className="run-timeline">
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
