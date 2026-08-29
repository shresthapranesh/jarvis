import * as stylex from '@stylexjs/stylex';
import {useState} from 'react';

import type {WorkerInfo} from '../hooks/useTaskEvents';
import {describeStep} from '../lib/steps';
import {colors, type} from '../theme/tokens.stylex';
import {worker} from './ui';

const RESULT_PREVIEW = 280;

// Describe what a worker is doing right now. Running workers reuse
// describeStep() — worker_step events carry the same node/data shape as
// main-agent steps, so "Calling run_cell(…)" / "Ran read_file(…)" labels
// come for free.
function workerActivity(w: WorkerInfo): string {
  if (w.status === 'error') return 'Failed';
  if (w.status === 'done') return 'Done';
  if (!w.node) return 'Starting…';
  return describeStep({
    id: String(w.idx),
    node: w.node,
    source: 'subagent',
    subagent: null,
    data: w.stepData,
    seq: 0,
    created_at: '',
  });
}

const DOT_STYLE: Record<string, keyof typeof worker> = {
  running: 'dotRunning',
  done: 'dotDone',
  error: 'dotError',
};

// The prop is `w`, not `worker`: the shared worker styles are imported under
// that name.
function WorkerCard({w}: {w: WorkerInfo}) {
  const [expanded, setExpanded] = useState(false);
  const running = w.status === 'running';
  const result = w.result ?? '';
  const clipped = !expanded && result.length > RESULT_PREVIEW;
  const expandable = result.length > RESULT_PREVIEW;

  return (
    <div {...stylex.props(styles.card, running && styles.cardRunning)}>
      <div {...stylex.props(styles.head)}>
        <span {...stylex.props(worker.dot, worker[DOT_STYLE[w.status] ?? 'dotUnknown'])} />
        <span {...stylex.props(worker.role)}>{w.role}</span>
        <span {...stylex.props(worker.task)} title={w.task}>
          {w.task}
        </span>
      </div>
      <div {...stylex.props(styles.activity)}>{workerActivity(w)}</div>
      {running && w.tail && <div {...stylex.props(styles.tail)}>{w.tail}</div>}
      {!running && result && (
        <div
          {...stylex.props(
            styles.result,
            clipped && styles.resultClipped,
            expandable && styles.resultExpandable,
          )}
          onClick={() => expandable && setExpanded((v) => !v)}
          title={clipped ? 'Click to expand' : undefined}
        >
          {clipped ? result.slice(0, RESULT_PREVIEW) + '…' : result}
        </div>
      )}
    </div>
  );
}

export function WorkerPanel({workers}: {workers: WorkerInfo[]}) {
  if (workers.length === 0) return null;
  const running = workers.filter((w) => w.status === 'running').length;
  const label =
    running > 0
      ? `${running} of ${workers.length} worker${workers.length !== 1 ? 's' : ''} running`
      : `${workers.length} worker${workers.length !== 1 ? 's' : ''} finished`;

  return (
    <div {...stylex.props(styles.panel)}>
      <div {...stylex.props(styles.panelHeader)}>{label}</div>
      {workers.map((w) => (
        <WorkerCard key={w.idx} w={w} />
      ))}
    </div>
  );
}

const styles = stylex.create({
  panel: {display: 'flex', flexDirection: 'column', gap: 6, marginBlock: '4px 8px'},
  panelHeader: {
    fontSize: type.tSmall,
    color: colors.textDim,
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
  },

  card: {
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    backgroundColor: colors.surface,
    paddingBlock: 8,
    paddingInline: 10,
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  },
  cardRunning: {borderColor: colors.borderStrong},

  head: {display: 'flex', alignItems: 'center', gap: 7, minWidth: 0},

  activity: {fontSize: type.tSmall, color: colors.textDim, paddingInlineStart: 14},
  tail: {
    fontSize: type.tSmall,
    color: colors.textDim,
    fontFamily: type.mono,
    lineHeight: 1.45,
    maxHeight: 60,
    overflow: 'hidden',
    display: 'flex',
    flexDirection: 'column-reverse', // keep the newest text visible
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    paddingInlineStart: 14,
    opacity: 0.85,
  },
  result: {
    fontSize: type.tSmall,
    lineHeight: 1.5,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    paddingInlineStart: 14,
    cursor: 'default',
  },
  resultClipped: {opacity: 0.9},
  resultExpandable: {cursor: 'pointer'},
});
