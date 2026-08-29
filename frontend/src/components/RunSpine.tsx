import * as stylex from '@stylexjs/stylex';
import {useEffect, useRef, useState} from 'react';

import type {WorkerInfo} from '../hooks/useTaskEvents';
import {compactNumber} from '../lib/format';
import type {Step} from '../lib/types';
import {foot, mini, node as nodeStyles, rail, track} from './RunSpine.styles';

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

  // A live run always shows the full column; a settled one is a 30px trace
  // until you point at it. Hover is state rather than a `:hover` rule because
  // the two are separate renders — see the note in RunSpine.styles.ts. The
  // rail is `display: none` below `bp.wide`, so there is no pointerless
  // viewport to strand here; a click still opens the full sidebar, which is
  // the better answer on a touchscreen anyway.
  const [hovered, setHovered] = useState(false);
  const expanded = isLive || hovered;

  // Newest node is at the bottom; follow it while the run is live.
  useEffect(() => {
    if (!isLive) return;
    const el = trackRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [steps.length, isLive]);

  const hidden = Math.max(0, steps.length - MAX_NODES);
  const visible = steps.slice(-MAX_NODES);
  const runningWorkers = workers.filter((w) => w.status === 'running').length;

  if (!expanded) {
    return (
      <aside
        {...stylex.props(rail.root, rail.rootCollapsed)}
        aria-label={`Run activity — ${steps.length} step${steps.length === 1 ? '' : 's'}`}
        onMouseEnter={() => setHovered(true)}
        onClick={onExpand}
      >
        <div {...stylex.props(mini.track)}>
          {visible.map((step) => (
            <span
              key={step.id}
              {...stylex.props(nodeStyles.mark, markForKind(stepKind(step)))}
              aria-hidden="true"
            />
          ))}
        </div>
        <span {...stylex.props(mini.count)}>{steps.length}</span>
      </aside>
    );
  }

  return (
    <aside
      {...stylex.props(rail.root)}
      aria-label="Run activity"
      onMouseLeave={() => setHovered(false)}
    >
      <header {...stylex.props(rail.head)}>
        <span {...stylex.props(rail.eyebrow)}>Run</span>
        <span {...stylex.props(rail.state, isLive && rail.stateLive)}>
          {isLive ? 'live' : 'settled'}
        </span>
      </header>

      <div {...stylex.props(track.root)} ref={trackRef}>
        {hidden > 0 && (
          <button {...stylex.props(track.earlier)} onClick={onExpand}>
            {hidden} earlier
          </button>
        )}

        {visible.length === 0 && !isLive && <p {...stylex.props(track.empty)}>No activity yet.</p>}

        {/* The connecting hairline lives on this wrapper, not the scroll
            container, so it ends at the last node instead of dangling. */}
        <div {...stylex.props(track.nodes)}>
          {visible.map((step, i) => {
            const kind = stepKind(step);
            const isLast = i === visible.length - 1;
            return (
              <button
                key={step.id}
                {...stylex.props(nodeStyles.root)}
                onClick={onExpand}
                title={spineLabel(step)}
              >
                <span
                  {...stylex.props(
                    nodeStyles.mark,
                    markForKind(kind),
                    isLive && isLast && nodeStyles.markActive,
                  )}
                  aria-hidden="true"
                />
                <span {...stylex.props(nodeStyles.label)}>{spineLabel(step)}</span>
              </button>
            );
          })}

          {isLive && (
            <div {...stylex.props(nodeStyles.root, nodeStyles.pending)}>
              <span {...stylex.props(nodeStyles.mark, nodeStyles.markActive)} aria-hidden="true" />
              <span {...stylex.props(nodeStyles.label)}>working…</span>
            </div>
          )}
        </div>
      </div>

      <footer {...stylex.props(foot.root)}>
        <dl {...stylex.props(foot.stats)}>
          <div {...stylex.props(foot.stat)}>
            <dt {...stylex.props(foot.key)}>steps</dt>
            <dd {...stylex.props(foot.value)}>{steps.length}</dd>
          </div>
          {runningWorkers > 0 && (
            <div {...stylex.props(foot.stat)}>
              <dt {...stylex.props(foot.key)}>workers</dt>
              <dd {...stylex.props(foot.value, foot.valueWorkers)}>{runningWorkers}</dd>
            </div>
          )}
          {artifactCount > 0 && (
            <div {...stylex.props(foot.stat)}>
              <dt {...stylex.props(foot.key)}>artifacts</dt>
              <dd {...stylex.props(foot.value)}>{artifactCount}</dd>
            </div>
          )}
          {budget && budget.totalTokens > 0 && (
            <div {...stylex.props(foot.stat)}>
              <dt {...stylex.props(foot.key)}>tokens</dt>
              <dd {...stylex.props(foot.value)}>{compactNumber(budget.totalTokens)}</dd>
            </div>
          )}
        </dl>
        <button {...stylex.props(foot.expand)} onClick={onExpand}>
          Open details
        </button>
      </footer>
    </aside>
  );
}

/** Node kind → mark colour. Declared beside the styles it selects from. */
function markForKind(kind: SpineKind) {
  switch (kind) {
    case 'tool':
      return nodeStyles.markTool;
    case 'worker':
      return nodeStyles.markWorker;
    case 'artifact':
      return nodeStyles.markArtifact;
    default:
      return nodeStyles.markThink;
  }
}
