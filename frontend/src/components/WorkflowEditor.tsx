import '@xyflow/react/dist/style.css';
import * as stylex from '@stylexjs/stylex';
import {
  addEdge,
  Background,
  Controls,
  Handle,
  MiniMap,
  Position,
  ReactFlow,
  useEdgesState,
  useNodesState,
  useReactFlow,
  type Connection,
  type NodeProps,
} from '@xyflow/react';
import {useCallback, useEffect, useState} from 'react';

import {useModels} from '../hooks/useModels';
import type {
  NodeStatus,
  Workflow,
  WorkflowNodeData,
  WorkflowNodeType,
  WorkflowRFEdge,
  WorkflowRFNode,
} from '../lib/types';
import {fetchWorkflowList} from '../relay/WorkflowListQuery';
import {config, editor, node as nodeStyles, palette, wfBtn} from './workflow.styles';

// ── Helpers ───────────────────────────────────────────────────────────────────

function extractTemplatePorts(template: string): string[] {
  const vars = [...template.matchAll(/\{\{(.+?)\}\}/g)].map((m) => m[1].trim().split(/[|.\s]/)[0]);
  return [...new Set(vars)];
}

function inputsToText(inputs: Record<string, string> | undefined): string {
  if (!inputs) return '';
  return Object.entries(inputs)
    .map(([k, v]) => (v ? `${k}=${v}` : k))
    .join('\n');
}

function textToInputs(text: string): Record<string, string> {
  const result: Record<string, string> = {};
  for (const line of text.split('\n')) {
    const t = line.trim();
    if (!t) continue;
    const eq = t.indexOf('=');
    result[eq >= 0 ? t.slice(0, eq).trim() : t] = eq >= 0 ? t.slice(eq + 1) : '';
  }
  return result;
}

function safeJsonParse(str: string, fallback: any = null): any {
  try {
    return JSON.parse(str);
  } catch {
    return fallback;
  }
}

// ── Custom node components ────────────────────────────────────────────────────

// The four accents the stylesheet actually defined; every other node type
// fell through to the plain card and still does.
type NodeAccent = 'agent' | 'cond' | 'start' | 'map' | null;

const ACCENT = {
  agent: nodeStyles.accentAgent,
  cond: nodeStyles.accentCond,
  start: nodeStyles.accentStart,
  map: nodeStyles.accentMap,
} as const;

const EXEC = {
  running: nodeStyles.running,
  done: nodeStyles.done,
  error: nodeStyles.error,
} as const;

/**
 * The card wrapper's props. Exec status comes last so it wins the box-shadow
 * over `selected` — that ordering is what the old rules' `!important` bought.
 */
function baseNodeProps(selected: boolean, execStatus?: string, accent: NodeAccent = null) {
  return stylex.props(
    nodeStyles.root,
    accent && ACCENT[accent],
    selected && nodeStyles.selected,
    execStatus ? EXEC[execStatus as keyof typeof EXEC] : null,
  );
}

function StartNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const keys = Object.keys((d.initial_inputs as Record<string, string>) ?? {});
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus, 'start')}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge, nodeStyles.badgeStart)}>start</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || 'Start'}</span>
      </div>
      {keys.length > 0 && <div {...stylex.props(nodeStyles.preview)}>{keys.join(', ')}</div>}
      {keys.length > 0 ? (
        keys.map((key, i) => (
          <Handle
            key={key}
            type="source"
            position={Position.Bottom}
            id={key}
            style={{left: `${((i + 1) / (keys.length + 1)) * 100}%`}}
            className="wf-handle"
          />
        ))
      ) : (
        <Handle type="source" position={Position.Bottom} id="output" className="wf-handle" />
      )}
    </div>
  );
}

function AgentNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const ports = extractTemplatePorts((d.prompt_template as string) || '');
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus, 'agent')}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge)}>agent</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || 'Agent'}</span>
      </div>
      {d.prompt_template && (
        <div {...stylex.props(nodeStyles.preview)}>
          {String(d.prompt_template).slice(0, 60)}
          {String(d.prompt_template).length > 60 ? '…' : ''}
        </div>
      )}
      {ports.map((port, i) => (
        <Handle
          key={port}
          type="target"
          position={Position.Top}
          id={port}
          style={{left: `${((i + 1) / (ports.length + 1)) * 100}%`}}
          className="wf-handle"
        />
      ))}
      {ports.length === 0 && (
        <Handle type="target" position={Position.Top} id="input" className="wf-handle" />
      )}
      <Handle
        type="source"
        position={Position.Bottom}
        id={(d.output_key as string) || 'result'}
        className="wf-handle"
      />
    </div>
  );
}

function ConditionalNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus, 'cond')}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge, nodeStyles.badgeCond)}>if</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || 'Condition'}</span>
      </div>
      {d.condition && (
        <div {...stylex.props(nodeStyles.preview)}>{String(d.condition).slice(0, 60)}</div>
      )}
      <Handle
        type="target"
        position={Position.Top}
        id={(d.input_key as string) || 'input'}
        className="wf-handle"
      />
      <Handle
        type="source"
        position={Position.Bottom}
        id="true"
        style={{left: '30%'}}
        className="wf-handle wf-handle--true"
      />
      <span {...stylex.props(nodeStyles.condLabel, nodeStyles.condTrue)}>true</span>
      <Handle
        type="source"
        position={Position.Bottom}
        id="false"
        style={{left: '70%'}}
        className="wf-handle wf-handle--false"
      />
      <span {...stylex.props(nodeStyles.condLabel, nodeStyles.condFalse)}>false</span>
    </div>
  );
}

function MapNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const execStatus = (d as any)._execStatus as string | undefined;
  const progress = (d as any)._mapProgress as {completed: number; total: number} | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus, 'map')}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge, nodeStyles.badgeMap)}>map</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || 'Map'}</span>
      </div>
      <div {...stylex.props(nodeStyles.preview)}>
        {d.items_key ? `each ${String(d.items_key)}` : 'configure items_key'}
        {progress && (
          <span {...stylex.props(nodeStyles.mapProgress)}>
            {' '}
            · {progress.completed}/{progress.total}
          </span>
        )}
      </div>
      <Handle
        type="target"
        position={Position.Top}
        id={(d.items_key as string) || 'items'}
        className="wf-handle"
      />
      <Handle
        type="source"
        position={Position.Bottom}
        id={(d.result_key as string) || 'results'}
        className="wf-handle"
      />
    </div>
  );
}

function RouterNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const cats = (d.categories as string[]) || [];
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus)}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge)}>router</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || 'Router'}</span>
      </div>
      <div {...stylex.props(nodeStyles.preview)}>
        {cats.length ? cats.join(', ').slice(0, 60) : 'add categories'}
      </div>
      <Handle type="target" position={Position.Top} id="input" className="wf-handle" />
      {cats.length ? (
        cats.map((c: string, i: number) => (
          <Handle
            key={c}
            type="source"
            position={Position.Bottom}
            id={c}
            style={{left: `${((i + 1) / (cats.length + 1)) * 100}%`}}
            className="wf-handle"
          />
        ))
      ) : (
        <Handle type="source" position={Position.Bottom} id="output" className="wf-handle" />
      )}
    </div>
  );
}

function SharedMultiHandleNode({
  data,
  selected,
  badge,
  fallbackLabel,
}: NodeProps & {badge: string; fallbackLabel: string}) {
  const d = data as WorkflowNodeData;
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus)}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge)}>{badge}</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || fallbackLabel}</span>
      </div>
      <div {...stylex.props(nodeStyles.preview)}>
        {
          (
            (d.prompt_template as string) ||
            (d.instruction as string) ||
            (d as any).goal ||
            ''
          ).slice(0, 60) as string
        }
      </div>
      <Handle type="target" position={Position.Top} id="input" className="wf-handle" />
      <Handle
        type="source"
        position={Position.Bottom}
        id={(d.output_key as string) || 'result'}
        className="wf-handle"
      />
    </div>
  );
}

function ApprovalNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus)}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge)}>approval</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || 'Approval'}</span>
      </div>
      <div {...stylex.props(nodeStyles.preview)}>
        {((d.reason as string) || 'needs approval').slice(0, 60)}
      </div>
      <Handle type="target" position={Position.Top} id="input" className="wf-handle" />
      <Handle
        type="source"
        position={Position.Bottom}
        id="approved"
        style={{left: '30%'}}
        className="wf-handle wf-handle--true"
      />
      <span {...stylex.props(nodeStyles.condLabel, nodeStyles.condTrue)}>approved</span>
      <Handle
        type="source"
        position={Position.Bottom}
        id="denied"
        style={{left: '70%'}}
        className="wf-handle wf-handle--false"
      />
      <span {...stylex.props(nodeStyles.condLabel, nodeStyles.condFalse)}>denied</span>
    </div>
  );
}

function HumanInputNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus)}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge)}>human</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || 'Human Input'}</span>
      </div>
      <div {...stylex.props(nodeStyles.preview)}>
        {((d.prompt as string) || (d.question as string) || 'ask user').slice(0, 60)}
      </div>
      <Handle type="target" position={Position.Top} id="input" className="wf-handle" />
      <Handle
        type="source"
        position={Position.Bottom}
        id={(d.output_key as string) || 'answer'}
        className="wf-handle"
      />
    </div>
  );
}

function PlannerNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus)}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge)}>plan</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || 'Planner'}</span>
      </div>
      <div {...stylex.props(nodeStyles.preview)}>
        {((d.prompt_template as string) || (d as any).goal || 'create plan').slice(0, 60)}
      </div>
      <Handle type="target" position={Position.Top} id="input" className="wf-handle" />
      <Handle
        type="source"
        position={Position.Bottom}
        id={(d.output_key as string) || 'plan'}
        className="wf-handle"
      />
    </div>
  );
}

function SequentialNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const steps = (d.steps as any[]) || [];
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus)}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge)}>seq</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || `Seq (${steps.length})`}</span>
      </div>
      <div {...stylex.props(nodeStyles.preview)}>
        {steps.length ? `${steps.length} steps` : 'add steps JSON'}
      </div>
      <Handle type="target" position={Position.Top} id="input" className="wf-handle" />
      <Handle
        type="source"
        position={Position.Bottom}
        id={(d.output_key as string) || 'result'}
        className="wf-handle"
      />
    </div>
  );
}

function ParallelNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const branches = (d.branches as any[]) || [];
  const execStatus = (d as any)._execStatus as string | undefined;
  return (
    <div {...baseNodeProps(!!selected, execStatus)}>
      <div {...stylex.props(nodeStyles.header)}>
        <span {...stylex.props(nodeStyles.badge)}>par</span>
        <span {...stylex.props(nodeStyles.label)}>{d.label || `Par (${branches.length})`}</span>
      </div>
      <div {...stylex.props(nodeStyles.preview)}>
        {branches.length ? `${branches.length} branches` : 'add branches JSON'}
      </div>
      <Handle type="target" position={Position.Top} id="input" className="wf-handle" />
      <Handle type="source" position={Position.Bottom} id="result" className="wf-handle" />
    </div>
  );
}

export const nodeTypes: Record<string, any> = {
  start: StartNode,
  agent: AgentNode,
  conditional: ConditionalNode,
  map: MapNode,
  router: RouterNode,
  refine: (p: NodeProps) => SharedMultiHandleNode({...p, badge: 'refine', fallbackLabel: 'Refine'}),
  sequential: SequentialNode,
  parallel: ParallelNode,
  loop: (p: NodeProps) => SharedMultiHandleNode({...p, badge: 'loop', fallbackLabel: 'Loop'}),
  approval: ApprovalNode,
  human_input: HumanInputNode,
  planner: PlannerNode,
  plan: PlannerNode,
};

// ── Config panel ──────────────────────────────────────────────────────────────

interface ConfigPanelProps {
  node: WorkflowRFNode;
  models: string[];
  onUpdate: (patch: Partial<WorkflowNodeData>) => void;
  onDelete: () => void;
}

function ConfigPanel({node, models, onUpdate, onDelete}: ConfigPanelProps) {
  const d = node.data;
  const type = node.type as WorkflowNodeType;

  const [mapMode, setMapMode] = useState<'workflow' | 'inline'>(
    d.workflow_id ? 'workflow' : 'inline',
  );
  const [savedWorkflows, setSavedWorkflows] = useState<Workflow[]>([]);
  const [showResilience, setShowResilience] = useState(false);
  const [showOutputSchema, setShowOutputSchema] = useState(!!d.output_schema);

  useEffect(() => {
    if (type !== 'map') return;
    fetchWorkflowList()
      .then(setSavedWorkflows)
      .catch(() => {});
  }, [type]);

  function switchMapMode(mode: 'workflow' | 'inline') {
    setMapMode(mode);
    if (mode === 'workflow') onUpdate({sub_graph: undefined});
    else onUpdate({workflow_id: undefined});
  }

  const titleMap: Record<string, string> = {
    start: 'Start Node',
    agent: 'Agent Node',
    conditional: 'Conditional Node',
    map: 'Map Node',
    router: 'Router Node (N-way)',
    refine: 'Refine Node (generate→evaluate)',
    sequential: 'Sequential Node',
    parallel: 'Parallel Node',
    loop: 'Loop Node',
    approval: 'Approval Node (HITL)',
    human_input: 'Human Input Node',
    planner: 'Planner Node',
    plan: 'Planner Node',
  };

  return (
    <div {...stylex.props(config.panel)}>
      <div {...stylex.props(config.panelTitle)}>{titleMap[type] || `${type} Node`}</div>

      <div {...stylex.props(config.field)}>
        <label {...stylex.props(config.label)}>Label</label>
        <input
          {...stylex.props(config.input)}
          value={(d.label as string) || ''}
          onChange={(e) => onUpdate({label: e.target.value})}
        />
      </div>

      {type === 'start' && (
        <div {...stylex.props(config.field)}>
          <label {...stylex.props(config.label)}>Input Keys & Defaults</label>
          <div {...stylex.props(config.hint)}>
            One per line · key or key=default — supports {'{{inputs.*}}'} and {'{{nodes.*}}'}
          </div>
          <textarea
            {...stylex.props(config.input, config.textarea)}
            rows={5}
            value={inputsToText(d.initial_inputs as any)}
            onChange={(e) => onUpdate({initial_inputs: textToInputs(e.target.value)})}
            placeholder={'topic\ncontext=some default'}
          />
        </div>
      )}

      {type !== 'start' && (
        <div {...stylex.props(config.field)}>
          <label {...stylex.props(config.label)}>Model</label>
          <select
            {...stylex.props(config.input)}
            value={(d.model as string) || ''}
            onChange={(e) => onUpdate({model: e.target.value})}
          >
            <option value="">Default</option>
            {models.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
          <div {...stylex.props(config.hint)}>Leave empty for default model</div>
        </div>
      )}

      {(type === 'agent' ||
        type === 'refine' ||
        type === 'loop' ||
        type === 'planner' ||
        type === 'plan') && (
        <div {...stylex.props(config.field)}>
          <label {...stylex.props(config.label)}>Prompt Template</label>
          <div {...stylex.props(config.hint)}>
            Jinja: {'{{var}}'}, {'{{inputs.foo}}'}, {'{{nodes.id.port}}'}, {'{{workflow.foo}}'} +
            filters |upper |json
          </div>
          <textarea
            {...stylex.props(config.input, config.textarea)}
            value={(d.prompt_template as string) || (d as any).goal || ''}
            rows={type === 'planner' ? 3 : 4}
            onChange={(e) =>
              onUpdate(
                type === 'planner'
                  ? {prompt_template: e.target.value, goal: e.target.value}
                  : {prompt_template: e.target.value},
              )
            }
            placeholder={
              type === 'planner'
                ? 'Goal to break into steps'
                : 'Use {{variable}}; {{nodes.n1.result}}'
            }
          />
        </div>
      )}

      {type === 'agent' && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Output Key</label>
            <input
              {...stylex.props(config.input)}
              value={(d.output_key as string) || ''}
              onChange={(e) => onUpdate({output_key: e.target.value})}
              placeholder="e.g. result"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.checkbox)}>
              <input
                type="checkbox"
                checked={showOutputSchema}
                onChange={(e) => setShowOutputSchema(e.target.checked)}
              />{' '}
              Structured output (JSON Schema)
            </label>
            {showOutputSchema && (
              <>
                <textarea
                  {...stylex.props(config.input, config.textarea)}
                  rows={4}
                  value={(d.output_schema as string) || ''}
                  onChange={(e) => onUpdate({output_schema: e.target.value})}
                  placeholder='{"type":"object","properties":{"title":{"type":"string"}}}'
                />
                <select
                  {...stylex.props(config.input)}
                  value={(d.output_schema_mode as string) || 'auto'}
                  onChange={(e) => onUpdate({output_schema_mode: e.target.value as any})}
                >
                  <option value="auto">auto (fallback to text)</option>
                  <option value="strict">strict (error if no JSON)</option>
                </select>
              </>
            )}
          </div>
        </>
      )}

      {type === 'conditional' && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Condition</label>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              value={(d.condition as string) || ''}
              rows={4}
              onChange={(e) => onUpdate({condition: e.target.value})}
              placeholder="Is {{research}} comprehensive? Use {{nodes.n1.result}}"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Input Key</label>
            <input
              {...stylex.props(config.input)}
              value={(d.input_key as string) || ''}
              onChange={(e) => onUpdate({input_key: e.target.value})}
              placeholder="e.g. research"
            />
          </div>
        </>
      )}

      {type === 'router' && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Categories (comma-separated)</label>
            <input
              {...stylex.props(config.input)}
              value={
                Array.isArray(d.categories)
                  ? d.categories.join(', ')
                  : d.categories == null
                    ? ''
                    : String(d.categories)
              }
              onChange={(e) =>
                onUpdate({
                  categories: e.target.value
                    .split(',')
                    .map((s) => s.trim())
                    .filter(Boolean) as any,
                })
              }
              placeholder="bug, feature, question"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Instruction</label>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              rows={3}
              value={(d.instruction as string) || ''}
              onChange={(e) => onUpdate({instruction: e.target.value})}
              placeholder="Classify {{input}} into one of {{categories}}"
            />
          </div>
        </>
      )}

      {(type === 'refine' || type === 'loop') && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Rubric / Evaluation Criteria</label>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              rows={3}
              value={(d.rubric as string) || ''}
              onChange={(e) => onUpdate({rubric: e.target.value})}
              placeholder="Criteria to judge draft against"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Max Iterations</label>
            <input
              {...stylex.props(config.input)}
              type="number"
              min={1}
              max={10}
              value={(d.max_iterations as number) || ''}
              onChange={(e) =>
                onUpdate({max_iterations: e.target.value ? Number(e.target.value) : undefined})
              }
              placeholder="3"
            />
          </div>
          {type === 'loop' && (
            <div {...stylex.props(config.field)}>
              <label {...stylex.props(config.label)}>Exit token</label>
              <input
                {...stylex.props(config.input)}
                value={(d.exit_on as string) || ''}
                onChange={(e) => onUpdate({exit_on: e.target.value})}
                placeholder="PASS"
              />
            </div>
          )}
          {type === 'refine' || type === 'loop' ? (
            <div {...stylex.props(config.field)}>
              <label {...stylex.props(config.label)}>Output Key</label>
              <input
                {...stylex.props(config.input)}
                value={(d.output_key as string) || ''}
                onChange={(e) => onUpdate({output_key: e.target.value})}
                placeholder="result"
              />
            </div>
          ) : null}
        </>
      )}

      {type === 'sequential' && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Steps (JSON array)</label>
            <div {...stylex.props(config.hint)}>
              {'[{"prompt_template": "Research {{topic}}", "output_key": "research"}]'}
            </div>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              rows={6}
              value={
                typeof d.steps === 'string'
                  ? (d.steps as string)
                  : JSON.stringify(d.steps || [], null, 2)
              }
              onChange={(e) => {
                const parsed = safeJsonParse(e.target.value);
                if (parsed) onUpdate({steps: parsed});
                else onUpdate({steps: e.target.value as any});
              }}
              placeholder='[{"prompt_template": "...", "output_key": "..."}]'
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Final Output Key (optional)</label>
            <input
              {...stylex.props(config.input)}
              value={(d.output_key as string) || ''}
              onChange={(e) => onUpdate({output_key: e.target.value})}
              placeholder="report"
            />
          </div>
        </>
      )}

      {type === 'parallel' && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Branches (JSON array)</label>
            <div {...stylex.props(config.hint)}>{'[{"prompt_template": "Task A {{input}}"}]'}</div>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              rows={6}
              value={
                typeof d.branches === 'string'
                  ? (d.branches as string)
                  : JSON.stringify(d.branches || [], null, 2)
              }
              onChange={(e) => {
                const parsed = safeJsonParse(e.target.value);
                if (parsed) onUpdate({branches: parsed});
                else onUpdate({branches: e.target.value as any});
              }}
              placeholder='[{"prompt_template": "..."}]'
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Concurrency</label>
            <input
              {...stylex.props(config.input)}
              type="number"
              min={1}
              value={(d.concurrency as number) || ''}
              onChange={(e) =>
                onUpdate({concurrency: e.target.value ? Number(e.target.value) : undefined})
              }
              placeholder="unlimited"
            />
          </div>
        </>
      )}

      {type === 'approval' && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Reason</label>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              rows={2}
              value={(d.reason as string) || ''}
              onChange={(e) => onUpdate({reason: e.target.value})}
              placeholder="Approval required to deploy"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Tool label</label>
            <input
              {...stylex.props(config.input)}
              value={(d.tool as string) || ''}
              onChange={(e) => onUpdate({tool: e.target.value})}
              placeholder="defaults to node id"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Timeout seconds</label>
            <input
              {...stylex.props(config.input)}
              type="number"
              min={1}
              value={(d.timeout_seconds as number) || ''}
              onChange={(e) =>
                onUpdate({timeout_seconds: e.target.value ? Number(e.target.value) : undefined})
              }
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>On deny</label>
            <select
              {...stylex.props(config.input)}
              value={(d.on_deny as string) || 'error'}
              onChange={(e) => onUpdate({on_deny: e.target.value as any})}
            >
              <option value="error">error (stall branch)</option>
              <option value="continue">continue (branch to denied)</option>
            </select>
          </div>
        </>
      )}

      {type === 'human_input' && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Prompt / Question</label>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              rows={3}
              value={(d.prompt as string) || (d.question as string) || ''}
              onChange={(e) => onUpdate({prompt: e.target.value, question: e.target.value})}
              placeholder="What should we do next?"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Output Key</label>
            <input
              {...stylex.props(config.input)}
              value={(d.output_key as string) || ''}
              onChange={(e) => onUpdate({output_key: e.target.value})}
              placeholder="answer"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Timeout seconds</label>
            <input
              {...stylex.props(config.input)}
              type="number"
              min={1}
              value={(d.timeout_seconds as number) || ''}
              onChange={(e) =>
                onUpdate({timeout_seconds: e.target.value ? Number(e.target.value) : undefined})
              }
            />
          </div>
        </>
      )}

      {(type === 'planner' || type === 'plan') && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Goal</label>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              rows={3}
              value={((d as any).goal as string) || (d.prompt_template as string) || ''}
              onChange={(e) =>
                onUpdate({prompt_template: e.target.value, goal: e.target.value} as any)
              }
              placeholder="Break down task into steps"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Rubric / Constraints</label>
            <textarea
              {...stylex.props(config.input, config.textarea)}
              rows={2}
              value={(d.rubric as string) || ''}
              onChange={(e) => onUpdate({rubric: e.target.value})}
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Max Steps</label>
            <input
              {...stylex.props(config.input)}
              type="number"
              min={1}
              max={10}
              value={(d.max_steps as number) || ''}
              onChange={(e) =>
                onUpdate({max_steps: e.target.value ? Number(e.target.value) : undefined})
              }
              placeholder="5"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Output Key</label>
            <input
              {...stylex.props(config.input)}
              value={(d.output_key as string) || ''}
              onChange={(e) => onUpdate({output_key: e.target.value})}
              placeholder="plan"
            />
          </div>
        </>
      )}

      {type === 'map' && (
        <>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Items Key</label>
            <input
              {...stylex.props(config.input)}
              value={(d.items_key as string) || ''}
              onChange={(e) => onUpdate({items_key: e.target.value})}
              placeholder="e.g. items"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Result Key</label>
            <input
              {...stylex.props(config.input)}
              value={(d.result_key as string) || ''}
              onChange={(e) => onUpdate({result_key: e.target.value})}
              placeholder="results"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Concurrency</label>
            <input
              {...stylex.props(config.input)}
              type="number"
              min={1}
              value={(d.concurrency as number) || ''}
              onChange={(e) =>
                onUpdate({concurrency: e.target.value ? Number(e.target.value) : undefined})
              }
              placeholder="unlimited"
            />
          </div>
          <div {...stylex.props(config.field)}>
            <label {...stylex.props(config.label)}>Sub-workflow Source</label>
            <div {...stylex.props(config.modeToggle)}>
              <button
                {...stylex.props(config.modeBtn, mapMode === 'workflow' && config.modeBtnActive)}
                onClick={() => switchMapMode('workflow')}
                type="button"
              >
                Saved Workflow
              </button>
              <button
                {...stylex.props(config.modeBtn, mapMode === 'inline' && config.modeBtnActive)}
                onClick={() => switchMapMode('inline')}
                type="button"
              >
                Inline Graph
              </button>
            </div>
          </div>
          {mapMode === 'workflow' && (
            <div {...stylex.props(config.field)}>
              <div {...stylex.props(config.hint)}>Select a saved workflow to run for each item</div>
              <select
                {...stylex.props(config.input)}
                value={(d.workflow_id as string) || ''}
                onChange={(e) => onUpdate({workflow_id: e.target.value || undefined})}
              >
                <option value="">— choose a workflow —</option>
                {savedWorkflows.map((wf) => (
                  <option key={wf.id} value={wf.id}>
                    {wf.name}
                  </option>
                ))}
              </select>
            </div>
          )}
          {mapMode === 'inline' && (
            <div {...stylex.props(config.field)}>
              <div {...stylex.props(config.hint)}>Paste {'{"nodes":[], "edges":[]}'}</div>
              <textarea
                {...stylex.props(config.input, config.textarea)}
                rows={5}
                value={(d.sub_graph as string) || ''}
                onChange={(e) => onUpdate({sub_graph: e.target.value})}
                placeholder='{"nodes": [], "edges": []}'
              />
            </div>
          )}
        </>
      )}

      {/* Resilience section — for all node types except start */}
      {type !== 'start' && (
        <div {...stylex.props(config.section)}>
          <button
            type="button"
            {...stylex.props(config.sectionToggle)}
            onClick={() => setShowResilience(!showResilience)}
          >
            {showResilience ? '▼' : '▶'} Resilience (retry / timeout / on_error)
          </button>
          {showResilience && (
            <>
              <div {...stylex.props(config.field)}>
                <label {...stylex.props(config.label)}>Timeout seconds</label>
                <input
                  {...stylex.props(config.input)}
                  type="number"
                  min={1}
                  value={(d.timeout_seconds as number) || ''}
                  onChange={(e) =>
                    onUpdate({timeout_seconds: e.target.value ? Number(e.target.value) : undefined})
                  }
                  placeholder="no timeout"
                />
              </div>
              <div {...stylex.props(config.field)}>
                <label {...stylex.props(config.label)}>Retries</label>
                <input
                  {...stylex.props(config.input)}
                  type="number"
                  min={0}
                  max={10}
                  value={(d.retries as number) ?? ''}
                  onChange={(e) =>
                    onUpdate({retries: e.target.value === '' ? undefined : Number(e.target.value)})
                  }
                  placeholder="0"
                />
              </div>
              <div {...stylex.props(config.field)}>
                <label {...stylex.props(config.label)}>Retry delay seconds</label>
                <input
                  {...stylex.props(config.input)}
                  type="number"
                  min={0}
                  step={0.5}
                  value={(d.retry_delay_seconds as number) ?? ''}
                  onChange={(e) =>
                    onUpdate({
                      retry_delay_seconds:
                        e.target.value === '' ? undefined : Number(e.target.value),
                    })
                  }
                  placeholder="1"
                />
              </div>
              <div {...stylex.props(config.field)}>
                <label {...stylex.props(config.label)}>On error</label>
                <select
                  {...stylex.props(config.input)}
                  value={(d.on_error as string) || 'error'}
                  onChange={(e) => onUpdate({on_error: e.target.value as any})}
                >
                  <option value="error">error (stall branch)</option>
                  <option value="continue">continue (use fallback)</option>
                  <option value="skip">skip</option>
                </select>
                <div {...stylex.props(config.hint)}>continue = emit done with fallback_output</div>
              </div>
              <div {...stylex.props(config.field)}>
                <label {...stylex.props(config.label)}>Fallback output (JSON dict)</label>
                <textarea
                  {...stylex.props(config.input, config.textarea)}
                  rows={3}
                  value={
                    d.fallback_output == null
                      ? ''
                      : typeof d.fallback_output === 'object'
                        ? JSON.stringify(d.fallback_output, null, 2)
                        : String(d.fallback_output)
                  }
                  onChange={(e) => {
                    const parsed = safeJsonParse(e.target.value);
                    if (parsed && typeof parsed === 'object') onUpdate({fallback_output: parsed});
                    else onUpdate({fallback_output: e.target.value as any});
                  }}
                  placeholder='{"result": "default value"}'
                />
              </div>
            </>
          )}
        </div>
      )}

      <button {...stylex.props(wfBtn.del)} onClick={onDelete}>
        Delete Node
      </button>
    </div>
  );
}

// ── Main editor component ─────────────────────────────────────────────────────

export interface WorkflowEditorProps {
  initialNodes: WorkflowRFNode[];
  initialEdges: WorkflowRFEdge[];
  nodeStatuses: Record<string, NodeStatus>;
  onSave: (nodes: WorkflowRFNode[], edges: WorkflowRFEdge[]) => Promise<void>;
  onRun: (nodes: WorkflowRFNode[], edges: WorkflowRFEdge[]) => void;
  saving: boolean;
  running: boolean;
}

export function WorkflowEditor({
  initialNodes,
  initialEdges,
  nodeStatuses,
  onSave,
  onRun,
  saving,
  running,
}: WorkflowEditorProps) {
  const [nodes, setNodes, onNodesChange] = useNodesState<WorkflowRFNode>(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState<WorkflowRFEdge>(initialEdges);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const {screenToFlowPosition} = useReactFlow();
  const {data: catalog} = useModels();
  const models = catalog?.available.map((m) => m.id) ?? [];

  useEffect(() => {
    setNodes(initialNodes);
    setEdges(initialEdges);
  }, [initialNodes.length]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (Object.keys(nodeStatuses).length === 0) return;
    setNodes((ns) =>
      ns.map((n) => {
        const st = nodeStatuses[n.id];
        if (!st) return n;
        return {
          ...n,
          data: {
            ...n.data,
            _execStatus: st.status,
            ...(st.mapProgress ? {_mapProgress: st.mapProgress} : {}),
          },
        };
      }),
    );
  }, [nodeStatuses, setNodes]);

  const onConnect = useCallback(
    (connection: Connection) => setEdges((eds) => addEdge(connection, eds)),
    [setEdges],
  );

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      e.preventDefault();
      const type = e.dataTransfer.getData('nodeType') as WorkflowNodeType;
      if (!type) return;
      const pos = screenToFlowPosition({x: e.clientX, y: e.clientY});
      const defaults: Record<string, WorkflowNodeData> = {
        start: {label: 'Start', initial_inputs: {}},
        agent: {label: 'New Agent', prompt_template: 'Do {{input}}', output_key: 'result'},
        conditional: {label: 'Condition', condition: 'Is {{input}} valid?', input_key: 'input'},
        map: {label: 'Map', items_key: 'items', result_key: 'results'},
        router: {label: 'Router', categories: ['a', 'b'], instruction: 'Classify {{input}}'},
        refine: {
          label: 'Refine',
          prompt_template: 'Draft {{input}}',
          rubric: 'Quality',
          max_iterations: 3,
          output_key: 'result',
        },
        sequential: {
          label: 'Sequential',
          steps: [{prompt_template: 'Step 1 {{input}}', output_key: 'step1'}],
          output_key: 'result',
        },
        parallel: {
          label: 'Parallel',
          branches: [{prompt_template: 'Branch A'}, {prompt_template: 'Branch B'}],
        },
        loop: {
          label: 'Loop',
          prompt_template: 'Generate {{input}}',
          rubric: 'Quality check',
          max_iterations: 3,
          output_key: 'result',
        },
        approval: {
          label: 'Approval',
          reason: 'Approve to continue?',
          tool: 'deploy',
          on_deny: 'error',
        },
        human_input: {label: 'Ask Human', prompt: 'What should we do?', output_key: 'answer'},
        planner: {
          label: 'Planner',
          prompt_template: 'Plan how to {{goal}}',
          goal: 'achieve the task',
          max_steps: 5,
          output_key: 'plan',
        },
        plan: {
          label: 'Planner',
          prompt_template: 'Plan how to {{goal}}',
          goal: 'achieve the task',
          max_steps: 5,
          output_key: 'plan',
        },
      };
      const defaultData = defaults[type] || {label: `New ${type}`};
      const newNode: WorkflowRFNode = {
        id: `${type}-${Date.now()}`,
        type,
        position: pos,
        data: defaultData,
      };
      setNodes((ns) => [...(ns as WorkflowRFNode[]), newNode] as never[]);
    },
    [screenToFlowPosition, setNodes],
  );

  const onDragOver = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
  }, []);

  const selectedNode = (nodes as WorkflowRFNode[]).find((n) => n.id === selectedNodeId) ?? null;

  function updateNodeData(patch: Partial<WorkflowNodeData>) {
    setNodes(
      (ns) =>
        (ns as WorkflowRFNode[]).map((n) =>
          n.id === selectedNodeId ? {...n, data: {...n.data, ...patch}} : n,
        ) as never[],
    );
  }

  function deleteSelectedNode() {
    if (!selectedNodeId) return;
    setNodes((ns) => (ns as WorkflowRFNode[]).filter((n) => n.id !== selectedNodeId) as never[]);
    setEdges((es) => es.filter((e) => e.source !== selectedNodeId && e.target !== selectedNodeId));
    setSelectedNodeId(null);
  }

  async function handleSave() {
    await onSave(nodes as WorkflowRFNode[], edges as WorkflowRFEdge[]);
  }

  const paletteItems: Array<{type: WorkflowNodeType; label: string; hint: string}> = [
    {type: 'start', label: 'Start', hint: 'entry point'},
    {type: 'agent', label: 'Agent', hint: 'LLM + tools'},
    {type: 'planner', label: 'Planner', hint: 'plan steps (JSON)'},
    {type: 'conditional', label: 'Conditional', hint: 'true / false'},
    {type: 'router', label: 'Router', hint: 'N-way classify'},
    {type: 'sequential', label: 'Sequential', hint: 'steps in order'},
    {type: 'parallel', label: 'Parallel', hint: 'fan-out'},
    {type: 'loop', label: 'Loop', hint: 'iterate until PASS'},
    {type: 'refine', label: 'Refine', hint: 'gen → eval loop'},
    {type: 'map', label: 'Map', hint: 'per item sub-flow'},
    {type: 'approval', label: 'Approval', hint: 'human gate'},
    {type: 'human_input', label: 'Human Input', hint: 'ask user'},
  ];

  return (
    <div {...stylex.props(editor.inner)}>
      <div {...stylex.props(palette.root)}>
        <div {...stylex.props(palette.label)}>Nodes</div>
        {paletteItems.map((it) => (
          <div
            key={it.type}
            {...stylex.props(palette.item)}
            draggable
            onDragStart={(e) => e.dataTransfer.setData('nodeType', it.type)}
          >
            <strong>{it.label}</strong>
            <div {...stylex.props(palette.itemHint)}>{it.hint}</div>
          </div>
        ))}
        <div {...stylex.props(palette.hint)}>
          Drag nodes onto canvas — use {'{{nodes.id.port}}'} in prompts
        </div>
      </div>

      <div {...stylex.props(editor.canvas)} onDrop={onDrop} onDragOver={onDragOver}>
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onConnect={onConnect}
          onNodeClick={(_, node) => setSelectedNodeId(node.id)}
          onPaneClick={() => setSelectedNodeId(null)}
          fitView
          deleteKeyCode="Delete"
        >
          <Background />
          <Controls />
          <MiniMap nodeColor={() => 'var(--rf-surface2)'} maskColor="rgba(0,0,0,0.4)" />
        </ReactFlow>
      </div>

      {selectedNode && (
        <ConfigPanel
          node={selectedNode}
          models={models}
          onUpdate={updateNodeData}
          onDelete={deleteSelectedNode}
        />
      )}

      <div {...stylex.props(editor.canvasToolbar)}>
        <button {...stylex.props(wfBtn.save)} disabled={saving} onClick={handleSave}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          {...stylex.props(wfBtn.run)}
          disabled={running}
          onClick={() => onRun(nodes as WorkflowRFNode[], edges as WorkflowRFEdge[])}
        >
          {running ? '⏳ Running' : '▶ Run'}
        </button>
      </div>
    </div>
  );
}
