import {useEffect, useRef, useState} from 'react';

import type {Step, TodoItem} from '../lib/types';
import {TodoList} from './TodoList';

interface Props {
  steps: Step[];
  isLive?: boolean;
  todos?: TodoItem[];
  onClose: () => void;
}

function formatStepData(raw: string): string {
  try {
    return JSON.stringify(JSON.parse(raw), null, 2);
  } catch {
    return raw;
  }
}

function safeParse(raw: string | null | undefined): Record<string, unknown> {
  if (!raw) return {};
  try {
    const v = JSON.parse(raw);
    return typeof v === 'object' && v !== null ? (v as Record<string, unknown>) : {};
  } catch {
    return {};
  }
}

// ── Grouping ─────────────────────────────────────────────────────────────────
// Steps whose `subagent` is set ("<role>:<idx>") were produced inside a
// spawned worker; fold each worker's steps into one collapsible group placed
// at the position of its first step. A repeated worker_start with the same
// key (a second spawn_workers batch reusing idx 1..N) starts a fresh group
// rather than merging into the old one.

type WorkerStatus = 'running' | 'done' | 'error' | 'unknown';

interface WorkerGroup {
  key: string;
  role: string;
  idx: string | null;
  task: string | null;
  status: WorkerStatus;
  steps: Step[];
}

type RenderItem = {kind: 'step'; step: Step} | {kind: 'group'; group: WorkerGroup};

function parseWorkerKey(key: string): {role: string; idx: string | null} {
  const m = key.match(/^(.*):(\d+)$/);
  return m ? {role: m[1], idx: m[2]} : {role: key, idx: null};
}

function buildRenderList(steps: Step[], isLive: boolean): RenderItem[] {
  const items: RenderItem[] = [];
  const open = new Map<string, WorkerGroup>();

  for (const step of steps) {
    const key = step.subagent;
    if (!key) {
      items.push({kind: 'step', step});
      continue;
    }
    let group = open.get(key);
    if (!group || step.node === 'worker_start') {
      group = {
        key,
        ...parseWorkerKey(key),
        task: null,
        status: isLive ? 'running' : 'unknown',
        steps: [],
      };
      open.set(key, group);
      items.push({kind: 'group', group});
    }
    if (step.node === 'worker_start') {
      // Header carries the task; the row itself would be redundant.
      const d = safeParse(step.data);
      if (typeof d.task === 'string') group.task = d.task;
    } else if (step.node === 'worker_done') {
      const d = safeParse(step.data);
      group.status = d.status === 'error' ? 'error' : 'done';
      group.steps.push(step); // keep the row — it holds the worker's result
    } else {
      group.steps.push(step);
    }
  }
  return items;
}

// ── Rows ─────────────────────────────────────────────────────────────────────

function StepRow({step}: {step: Step}) {
  const [open, setOpen] = useState(false);
  const hasData = !!step.data && step.data !== '{}';

  return (
    <div className={`step-row${open ? ' open' : ''}`}>
      <div
        className="step-summary"
        onClick={() => hasData && setOpen((o) => !o)}
        style={{cursor: hasData ? 'pointer' : 'default'}}
      >
        <span className={`step-source ${step.source === 'subagent' ? 'sub' : 'main'}`}>
          {step.source}
        </span>
        <span className="step-node">{step.node}</span>
        {hasData && (
          <svg
            className="step-chevron"
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <polyline points="9 18 15 12 9 6" />
          </svg>
        )}
      </div>
      {hasData && open && (
        <div className="step-detail">
          <pre>{formatStepData(step.data!)}</pre>
        </div>
      )}
    </div>
  );
}

function WorkerGroupRow({group}: {group: WorkerGroup}) {
  const [open, setOpen] = useState(group.status === 'running');

  return (
    <div className={`worker-group${open ? ' open' : ''}`}>
      <div className="worker-group-head" onClick={() => setOpen((o) => !o)}>
        <span className={`worker-status-dot worker-status-dot--${group.status}`} />
        <span className="worker-role">
          {group.role}
          {group.idx != null && ` #${group.idx}`}
        </span>
        {group.task && (
          <span className="worker-task" title={group.task}>
            {group.task}
          </span>
        )}
        <span className="worker-group-count">{group.steps.length}</span>
        <svg
          className="step-chevron"
          width="12"
          height="12"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <polyline points="9 18 15 12 9 6" />
        </svg>
      </div>
      {open && (
        <div className="worker-group-body">
          {group.steps.length === 0 ? (
            <div className="sidebar-empty">Starting…</div>
          ) : (
            group.steps.map((s, i) => <StepRow key={i} step={s} />)
          )}
        </div>
      )}
    </div>
  );
}

export function ActivitySidebar({steps, isLive, todos, onClose}: Props) {
  const bodyRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom when new steps arrive during live streaming
  useEffect(() => {
    if (isLive && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [steps.length, isLive]);

  const items = buildRenderList(steps, !!isLive);

  return (
    <div className="steps-panel">
      <div className="steps-panel-header">
        <div className="steps-panel-title">
          <span>
            {steps.length} step{steps.length !== 1 ? 's' : ''}
          </span>
          {isLive && (
            <span className="live-badge">
              <span className="live-dot" />
              Live
            </span>
          )}
        </div>
        <button className="sidebar-close" onClick={onClose} title="Close">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
      <div className="steps-panel-body" ref={bodyRef}>
        {todos && todos.length > 0 && <TodoList todos={todos} compact />}
        {items.length === 0 ? (
          <div className="sidebar-empty">No activity recorded.</div>
        ) : (
          items.map((item, i) =>
            item.kind === 'step' ? (
              <StepRow key={i} step={item.step} />
            ) : (
              <WorkerGroupRow key={i} group={item.group} />
            ),
          )
        )}
      </div>
    </div>
  );
}
