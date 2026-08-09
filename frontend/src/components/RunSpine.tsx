import {useEffect, useRef} from 'react';

import type {WorkerInfo} from '../hooks/useTaskEvents';
import {compactNumber} from '../lib/format';
import type {Step} from '../lib/types';

interface Budget {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  llmCalls: number;
  toolCalls: number;
}

interface Props {
  steps: Step[];
  workers?: WorkerInfo[];
  artifactCount?: number;
  isLive?: boolean;
  budget?: Budget | null;
  onExpand: () => void;
}

// The rail is a *trace*, not a log — it shows the shape of the run at a
// glance. Anything needing full payloads goes to the ActivitySidebar, which
// this expands into.
const MAX_NODES = 16;

type SpineKind = 'tool' | 'worker' | 'think' | 'artifact';

function stepKind(step: Step): SpineKind {
  if (step.node === 'worker_start' || step.node === 'worker_done' || step.subagent) return 'worker';
  if (step.node === 'artifact') return 'artifact';
  if (step.node === 'tools') return 'tool';
  if (step.node === 'model_request') {
    try {
      if (step.data && JSON.parse(step.data)?.tool_calls?.[0]?.name) return 'tool';
    } catch {
      /* non-JSON payload — treat as reasoning */
    }
  }
  return 'think';
}

// "researcher:1" → "researcher #1"
function prettyWorker(key: string | null | undefined): string {
  if (!key) return 'worker';
  return key.replace(/:(\d+)$/, ' #$1');
}

/** Terse label — the rail is ~200px, so this must stay short. */
function spineLabel(step: Step): string {
  if (step.node === 'worker_start') return `spawn ${prettyWorker(step.subagent)}`;
  if (step.node === 'worker_done') return `${prettyWorker(step.subagent)} done`;
  if (step.subagent) return prettyWorker(step.subagent);
  if (step.node === 'artifact') return 'artifact';

  if (step.data) {
    try {
      const parsed = JSON.parse(step.data);
      const pending = parsed?.tool_calls?.[0]?.name;
      if (pending) return pending;
      if (step.node === 'tools') {
        const first = Array.isArray(parsed) ? parsed[0] : parsed;
        if (first?.tool) return first.tool;
      }
    } catch {
      /* fall through to the generic label */
    }
  }
  return step.node === 'tools' ? 'tool' : 'reasoning';
}

export function RunSpine({
  steps,
  workers = [],
  artifactCount = 0,
  isLive = false,
  budget,
  onExpand,
}: Props) {
  const trackRef = useRef<HTMLDivElement | null>(null);

  // Newest node is at the bottom; follow it while the run is live.
  useEffect(() => {
    if (!isLive) return;
    const el = trackRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [steps.length, isLive]);

  const hidden = Math.max(0, steps.length - MAX_NODES);
  const visible = steps.slice(-MAX_NODES);
  const runningWorkers = workers.filter((w) => w.status === 'running').length;

  return (
    <aside className="run-spine" aria-label="Run activity">
      <header className="spine-head">
        <span className="spine-eyebrow">Run</span>
        <span className={`spine-state${isLive ? ' spine-state--live' : ''}`}>
          {isLive ? 'live' : 'settled'}
        </span>
      </header>

      <div className="spine-track" ref={trackRef}>
        {hidden > 0 && (
          <button className="spine-earlier" onClick={onExpand}>
            {hidden} earlier
          </button>
        )}

        {visible.length === 0 && !isLive && <p className="spine-empty">No activity yet.</p>}

        {/* The connecting hairline lives on this wrapper, not the scroll
            container, so it ends at the last node instead of dangling. */}
        <div className="spine-nodes">
          {visible.map((step, i) => {
            const kind = stepKind(step);
            const isLast = i === visible.length - 1;
            return (
              <button
                key={step.id}
                className={`spine-node spine-node--${kind}${
                  isLive && isLast ? ' spine-node--active' : ''
                }`}
                onClick={onExpand}
                title={spineLabel(step)}
              >
                <span className="spine-mark" aria-hidden="true" />
                <span className="spine-label">{spineLabel(step)}</span>
              </button>
            );
          })}

          {isLive && (
            <div className="spine-node spine-node--pending">
              <span className="spine-mark" aria-hidden="true" />
              <span className="spine-label">working…</span>
            </div>
          )}
        </div>
      </div>

      <footer className="spine-foot">
        <dl className="spine-stats">
          <div className="spine-stat">
            <dt>steps</dt>
            <dd>{steps.length}</dd>
          </div>
          {runningWorkers > 0 && (
            <div className="spine-stat spine-stat--workers">
              <dt>workers</dt>
              <dd>{runningWorkers}</dd>
            </div>
          )}
          {artifactCount > 0 && (
            <div className="spine-stat">
              <dt>artifacts</dt>
              <dd>{artifactCount}</dd>
            </div>
          )}
          {budget && budget.totalTokens > 0 && (
            <div className="spine-stat">
              <dt>tokens</dt>
              <dd>{compactNumber(budget.totalTokens)}</dd>
            </div>
          )}
        </dl>
        <button className="spine-expand" onClick={onExpand}>
          Open details
        </button>
      </footer>
    </aside>
  );
}
