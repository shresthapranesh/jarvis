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

export function ActivitySidebar({steps, isLive, todos, onClose}: Props) {
  const bodyRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom when new steps arrive during live streaming
  useEffect(() => {
    if (isLive && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [steps.length, isLive]);

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
        {steps.length === 0 ? (
          <div className="sidebar-empty">No activity recorded.</div>
        ) : (
          steps.map((s, i) => <StepRow key={i} step={s} />)
        )}
      </div>
    </div>
  );
}
