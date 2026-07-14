import {useState} from 'react';

import type {WorkerInfo} from '../hooks/useTaskEvents';
import {describeStep} from '../lib/steps';

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

function WorkerCard({worker}: {worker: WorkerInfo}) {
  const [expanded, setExpanded] = useState(false);
  const running = worker.status === 'running';
  const result = worker.result ?? '';
  const clipped = !expanded && result.length > RESULT_PREVIEW;

  return (
    <div className={`worker-card worker-card--${worker.status}`}>
      <div className="worker-card-head">
        <span className={`worker-status-dot worker-status-dot--${worker.status}`} />
        <span className="worker-role">{worker.role}</span>
        <span className="worker-task" title={worker.task}>
          {worker.task}
        </span>
      </div>
      <div className="worker-activity">{workerActivity(worker)}</div>
      {running && worker.tail && <div className="worker-tail">{worker.tail}</div>}
      {!running && result && (
        <div
          className={`worker-result${clipped ? ' worker-result--clipped' : ''}`}
          onClick={() => result.length > RESULT_PREVIEW && setExpanded((v) => !v)}
          style={{cursor: result.length > RESULT_PREVIEW ? 'pointer' : 'default'}}
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
    <div className="worker-panel">
      <div className="worker-panel-header">{label}</div>
      {workers.map((w) => (
        <WorkerCard key={w.idx} worker={w} />
      ))}
    </div>
  );
}
