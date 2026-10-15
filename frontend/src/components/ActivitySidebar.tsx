import * as stylex from '@stylexjs/stylex';
import {useEffect, useMemo, useRef, useState} from 'react';

import {summarizeStep} from '../lib/steps';
import type {Step, TodoItem} from '../lib/types';
import {
  budget as budgetStyles,
  empty,
  group as groupStyles,
  panel,
  row,
} from './ActivitySidebar.styles';
import {TodoList} from './TodoList';
import {closeBtn, stream, worker as workerStyles} from './ui';

interface Budget {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  llmCalls: number;
  toolCalls: number;
}

interface Perf {
  ttftMs: number | null;
  llmMs: number | null;
  prefillTps: number | null;
  evalTps: number | null;
  llmCalls: number;
}

interface Props {
  steps: Step[];
  isLive?: boolean;
  todos?: TodoItem[];
  budget?: Budget | null;
  perf?: Perf | null;
  onClose: () => void;
}

function fmtRate(tps: number) {
  return tps >= 100 ? String(Math.round(tps)) : tps.toFixed(1);
}

function fmtMs(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`;
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

// ── Detail ───────────────────────────────────────────────────────────────────
// What an expanded row shows: the step's content in the form it was written
// in — a cell's code as code, a tool's output as text, a reply as prose —
// rather than the JSON envelope the event carried it in. Anything without a
// known shape still falls back to that JSON.

// Code wraps like output: the panel is 312px, and a cell scrolled sideways
// hides most of every line.
type Section = {caption?: string; text: string; as: 'pre' | 'prose'};

function asText(v: unknown): string {
  return typeof v === 'string' ? v : JSON.stringify(v, null, 2);
}

function stepSections(step: Step): Section[] | null {
  let parsed: any;
  try {
    parsed = step.data ? JSON.parse(step.data) : null;
  } catch {
    return null;
  }
  if (step.node === 'model_request') {
    const calls = Array.isArray(parsed?.tool_calls) ? parsed.tool_calls : [];
    if (calls.length > 0) {
      return calls.flatMap((call: any) => {
        const args = (call?.args ?? call?.input ?? {}) as Record<string, unknown>;
        const name = calls.length > 1 ? `${call?.name ?? 'call'} · ` : '';
        const entries = Object.entries(args);
        if (entries.length === 0) return [{caption: `${name}no arguments`, text: '', as: 'pre'}];
        return entries.map(([key, value]) => ({
          caption: `${name}${key}`,
          text: asText(value),
          as: 'pre',
        }));
      });
    }
    if (typeof parsed?.text === 'string') return [{text: parsed.text, as: 'prose'}];
  }
  if (step.node === 'tools') {
    const items = Array.isArray(parsed) ? parsed : [parsed];
    const outputs = items.filter((it: any) => it && 'output' in it);
    if (outputs.length > 0) {
      return outputs.map((it: any) => ({
        caption: items.length > 1 ? `${it.tool ?? 'tool'} output` : undefined,
        text: asText(it.output),
        as: 'pre',
      }));
    }
  }
  return null;
}

function StepDetail({step}: {step: Step}) {
  const sections = useMemo(() => stepSections(step), [step]);
  if (!sections) return <pre {...stylex.props(row.pre)}>{formatStepData(step.data!)}</pre>;
  return (
    <div {...stylex.props(row.sections)}>
      {sections.map((sec, i) => (
        <div key={i}>
          {sec.caption && <div {...stylex.props(row.caption)}>{sec.caption}</div>}
          {sec.text &&
            (sec.as === 'prose' ? (
              <p {...stylex.props(row.prose)}>{sec.text}</p>
            ) : (
              <pre {...stylex.props(row.pre)}>{sec.text}</pre>
            ))}
        </div>
      ))}
    </div>
  );
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

/** `nested` indents a row that sits inside a worker group. */
function StepRow({step, nested = false}: {step: Step; nested?: boolean}) {
  const [open, setOpen] = useState(false);
  const hasData = !!step.data && step.data !== '{}';
  const summary = useMemo(() => summarizeStep(step), [step]);

  return (
    <div {...stylex.props(row.root, nested && row.nested)}>
      <div
        {...stylex.props(row.summary, nested && row.inset, hasData ? row.clickable : row.plain)}
        onClick={() => hasData && setOpen((o) => !o)}
        title={summary.detail ?? undefined}
      >
        {/* Every step is the main agent's unless it says otherwise; a worker's
            steps already sit inside its group, so the tag only marks strays. */}
        {step.source !== 'main' && !nested && (
          <span {...stylex.props(row.source, row.sourceSub)}>{step.source}</span>
        )}
        <span
          {...stylex.props(
            row.kind,
            summary.kind === 'call' && row.kindCall,
            summary.kind === 'other' && row.kindOther,
          )}
        >
          {summary.label}
        </span>
        {summary.detail && (
          <span {...stylex.props(row.detailText, summary.kind === 'output' && row.detailDim)}>
            {summary.detail}
          </span>
        )}
        {hasData && (
          <svg
            {...stylex.props(row.chevron, open && row.chevronOpen)}
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
        <div {...stylex.props(row.detail, nested && row.inset)}>
          <StepDetail step={step} />
        </div>
      )}
    </div>
  );
}

function dotStyle(status: WorkerStatus) {
  if (status === 'running') return workerStyles.dotRunning;
  if (status === 'done') return workerStyles.dotDone;
  if (status === 'error') return workerStyles.dotError;
  return workerStyles.dotUnknown;
}

function WorkerGroupRow({group}: {group: WorkerGroup}) {
  const [open, setOpen] = useState(group.status === 'running');

  return (
    <div {...stylex.props(groupStyles.root)}>
      <div {...stylex.props(groupStyles.head)} onClick={() => setOpen((o) => !o)}>
        <span {...stylex.props(workerStyles.dot, dotStyle(group.status))} />
        <span {...stylex.props(workerStyles.role)}>
          {group.role}
          {group.idx != null && ` #${group.idx}`}
        </span>
        {group.task && (
          <span {...stylex.props(workerStyles.task)} title={group.task}>
            {group.task}
          </span>
        )}
        <span {...stylex.props(groupStyles.count)}>{group.steps.length}</span>
        <svg
          {...stylex.props(row.chevron, row.chevronFlush, open && row.chevronOpen)}
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
        <div>
          {group.steps.length === 0 ? (
            <div {...stylex.props(empty.block, empty.inline)}>Starting…</div>
          ) : (
            group.steps.map((s, i) => <StepRow key={i} step={s} nested />)
          )}
        </div>
      )}
    </div>
  );
}

export function ActivitySidebar({steps, isLive, todos, budget, perf, onClose}: Props) {
  const bodyRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll to bottom when new steps arrive during live streaming
  useEffect(() => {
    if (isLive && bodyRef.current) {
      bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
    }
  }, [steps.length, isLive]);

  const items = buildRenderList(steps, !!isLive);

  return (
    <div {...stylex.props(panel.root)}>
      <div {...stylex.props(panel.header)}>
        <div {...stylex.props(panel.title)}>
          <span>
            {steps.length} step{steps.length !== 1 ? 's' : ''}
          </span>
          {isLive && (
            <span {...stylex.props(panel.live)}>
              <span {...stylex.props(stream.liveDot)} />
              Live
            </span>
          )}
        </div>
        <button {...stylex.props(closeBtn.base)} onClick={onClose} title="Close">
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
      <div {...stylex.props(panel.body)} ref={bodyRef}>
        {todos && todos.length > 0 && <TodoList todos={todos} compact />}
        {budget && (
          <div {...stylex.props(budgetStyles.box)}>
            <div {...stylex.props(budgetStyles.head)}>
              <strong>Budget</strong>
              {/* The threshold colour is data-driven, so it is chosen here
                  rather than encoded as three variants. */}
              <span
                {...stylex.props(
                  budget.totalTokens > 400_000
                    ? budgetStyles.over
                    : budget.totalTokens > 300_000
                      ? budgetStyles.warn
                      : budgetStyles.ok,
                )}
              >
                {budget.totalTokens.toLocaleString()} tokens
              </span>
            </div>
            <div {...stylex.props(budgetStyles.bar)}>
              <div
                {...stylex.props(
                  budgetStyles.fill,
                  budget.totalTokens > 400_000 && budgetStyles.fillOver,
                )}
                style={{
                  width: `${Math.min(100, Math.round((budget.totalTokens / 500_000) * 100))}%`,
                }}
              />
            </div>
            <div {...stylex.props(budgetStyles.stats)}>
              <span>{budget.inputTokens.toLocaleString()} in</span>
              <span>{budget.outputTokens.toLocaleString()} out</span>
              <span>{budget.llmCalls} llm</span>
              <span>{budget.toolCalls} tools</span>
            </div>
            {perf && (perf.ttftMs != null || perf.prefillTps != null || perf.evalTps != null) && (
              <div
                {...stylex.props(budgetStyles.stats, budgetStyles.perf)}
                title={
                  'Throughput across this run.\n' +
                  'TTFT: time to the first token of the first LLM call.\n' +
                  'pp: prompt processing (prefill), cache-read tokens excluded.\n' +
                  'tg: text generation.' +
                  (perf.llmMs != null
                    ? `\nTime in LLM calls: ${fmtMs(perf.llmMs)} (excludes tools).`
                    : '')
                }
              >
                {perf.ttftMs != null && <span>{fmtMs(perf.ttftMs)} TTFT</span>}
                {perf.prefillTps != null && <span>{fmtRate(perf.prefillTps)} tok/s pp</span>}
                {perf.evalTps != null && <span>{fmtRate(perf.evalTps)} tok/s tg</span>}
              </div>
            )}
          </div>
        )}
        {items.length === 0 ? (
          <div {...stylex.props(empty.block)}>No activity recorded.</div>
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
