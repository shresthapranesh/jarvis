import '@xyflow/react/dist/style.css';
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
import {listWorkflows} from '../lib/api';
import {useModels} from '../hooks/useModels';
import type {NodeStatus, Workflow, WorkflowNodeData, WorkflowNodeType, WorkflowRFEdge, WorkflowRFNode} from '../lib/types';

// ── Helpers ───────────────────────────────────────────────────────────────────

function extractTemplatePorts(template: string): string[] {
  const vars = [...template.matchAll(/\{\{(.+?)\}\}/g)].map((m) => m[1].trim());
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

// ── Custom node components ────────────────────────────────────────────────────

function StartNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const keys = Object.keys((d.initial_inputs as Record<string, string>) ?? {});
  const execStatus = d._execStatus as string | undefined;

  return (
    <div
      className={[
        'wf-node wf-node--start',
        selected ? 'selected' : '',
        execStatus ? `wf-node--${execStatus}` : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className="wf-node-header">
        <span className="wf-node-type-badge wf-node-type-badge--start">start</span>
        <span className="wf-node-label">{d.label || 'Start'}</span>
      </div>
      {keys.length > 0 && (
        <div className="wf-node-preview">{keys.join(', ')}</div>
      )}
      {keys.length > 0
        ? keys.map((key, i) => (
            <Handle
              key={key}
              type="source"
              position={Position.Bottom}
              id={key}
              style={{left: `${((i + 1) / (keys.length + 1)) * 100}%`}}
              className="wf-handle"
            />
          ))
        : (
            <Handle type="source" position={Position.Bottom} id="output" className="wf-handle" />
          )}
    </div>
  );
}

function AgentNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const ports = extractTemplatePorts((d.prompt_template as string) || '');
  const execStatus = d._execStatus as string | undefined;

  return (
    <div
      className={[
        'wf-node wf-node--agent',
        selected ? 'selected' : '',
        execStatus ? `wf-node--${execStatus}` : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className="wf-node-header">
        <span className="wf-node-type-badge">agent</span>
        <span className="wf-node-label">{d.label || 'Agent'}</span>
      </div>
      {d.prompt_template && (
        <div className="wf-node-preview">
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
  const execStatus = d._execStatus as string | undefined;

  return (
    <div
      className={[
        'wf-node wf-node--conditional',
        selected ? 'selected' : '',
        execStatus ? `wf-node--${execStatus}` : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className="wf-node-header">
        <span className="wf-node-type-badge wf-node-type-badge--cond">if</span>
        <span className="wf-node-label">{d.label || 'Condition'}</span>
      </div>
      {d.condition && (
        <div className="wf-node-preview">
          {String(d.condition).slice(0, 60)}
          {String(d.condition).length > 60 ? '…' : ''}
        </div>
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
      <span className="wf-cond-label wf-cond-label--true">true</span>
      <Handle
        type="source"
        position={Position.Bottom}
        id="false"
        style={{left: '70%'}}
        className="wf-handle wf-handle--false"
      />
      <span className="wf-cond-label wf-cond-label--false">false</span>
    </div>
  );
}

function MapNode({data, selected}: NodeProps) {
  const d = data as WorkflowNodeData;
  const execStatus = d._execStatus as string | undefined;
  const progress = d._mapProgress as {completed: number; total: number} | undefined;

  return (
    <div
      className={[
        'wf-node wf-node--map',
        selected ? 'selected' : '',
        execStatus ? `wf-node--${execStatus}` : '',
      ]
        .filter(Boolean)
        .join(' ')}
    >
      <div className="wf-node-header">
        <span className="wf-node-type-badge wf-node-type-badge--map">map</span>
        <span className="wf-node-label">{d.label || 'Map'}</span>
      </div>
      <div className="wf-node-preview">
        {d.items_key ? `each ${String(d.items_key)}` : 'configure items_key'}
        {progress && (
          <span className="wf-map-progress"> · {progress.completed}/{progress.total}</span>
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

export const nodeTypes = {
  start: StartNode,
  agent: AgentNode,
  conditional: ConditionalNode,
  map: MapNode,
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
  const isStart = node.type === 'start';
  const isAgent = node.type === 'agent';
  const isMap = node.type === 'map';

  const [mapMode, setMapMode] = useState<'workflow' | 'inline'>(
    d.workflow_id ? 'workflow' : 'inline'
  );
  const [savedWorkflows, setSavedWorkflows] = useState<Workflow[]>([]);

  useEffect(() => {
    if (!isMap) return;
    listWorkflows().then(setSavedWorkflows).catch(() => {});
  }, [isMap]);

  function switchMapMode(mode: 'workflow' | 'inline') {
    setMapMode(mode);
    if (mode === 'workflow') onUpdate({sub_graph: undefined});
    else onUpdate({workflow_id: undefined});
  }

  const panelTitle = isStart
    ? 'Start Node'
    : isAgent
      ? 'Agent Node'
      : isMap
        ? 'Map Node'
        : 'Conditional Node';

  return (
    <div className="wf-config-panel">
      <div className="wf-config-panel-title">{panelTitle}</div>

      <div className="wf-config-field">
        <label className="wf-config-label">Label</label>
        <input
          className="wf-config-input"
          value={(d.label as string) || ''}
          onChange={(e) => onUpdate({label: e.target.value})}
        />
      </div>

      {isStart && (
        <div className="wf-config-field">
          <label className="wf-config-label">Input Keys &amp; Defaults</label>
          <div className="wf-config-hint">One per line · key or key=default</div>
          <textarea
            className="wf-config-textarea"
            rows={5}
            value={inputsToText(d.initial_inputs as Record<string, string> | undefined)}
            onChange={(e) => onUpdate({initial_inputs: textToInputs(e.target.value)})}
            placeholder={'topic\ncontext=some default value'}
          />
        </div>
      )}

      {!isStart && (
        <div className="wf-config-field">
          <label className="wf-config-label">Model</label>
          <select
            className="wf-config-select"
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
        </div>
      )}

      {isAgent && (
        <>
          <div className="wf-config-field">
            <label className="wf-config-label">Prompt Template</label>
            <textarea
              className="wf-config-textarea"
              value={(d.prompt_template as string) || ''}
              rows={4}
              onChange={(e) => onUpdate({prompt_template: e.target.value})}
              placeholder="Use {{variable}} for inputs"
            />
          </div>
          <div className="wf-config-field">
            <label className="wf-config-label">Output Key</label>
            <input
              className="wf-config-input"
              value={(d.output_key as string) || ''}
              onChange={(e) => onUpdate({output_key: e.target.value})}
              placeholder="e.g. result"
            />
          </div>
        </>
      )}

      {!isStart && !isAgent && !isMap && (
        <>
          <div className="wf-config-field">
            <label className="wf-config-label">Condition</label>
            <textarea
              className="wf-config-textarea"
              value={(d.condition as string) || ''}
              rows={4}
              onChange={(e) => onUpdate({condition: e.target.value})}
              placeholder="Is the following comprehensive? {{input}}"
            />
          </div>
          <div className="wf-config-field">
            <label className="wf-config-label">Input Key</label>
            <input
              className="wf-config-input"
              value={(d.input_key as string) || ''}
              onChange={(e) => onUpdate({input_key: e.target.value})}
              placeholder="e.g. research"
            />
          </div>
        </>
      )}

      {isMap && (
        <>
          <div className="wf-config-field">
            <label className="wf-config-label">Items Key</label>
            <input
              className="wf-config-input"
              value={(d.items_key as string) || ''}
              onChange={(e) => onUpdate({items_key: e.target.value})}
              placeholder="e.g. items"
            />
          </div>
          <div className="wf-config-field">
            <label className="wf-config-label">Result Key</label>
            <input
              className="wf-config-input"
              value={(d.result_key as string) || ''}
              onChange={(e) => onUpdate({result_key: e.target.value})}
              placeholder="results"
            />
          </div>
          <div className="wf-config-field">
            <label className="wf-config-label">Concurrency</label>
            <input
              className="wf-config-input"
              type="number"
              min={1}
              value={(d.concurrency as number) || ''}
              onChange={(e) => onUpdate({concurrency: e.target.value ? Number(e.target.value) : undefined})}
              placeholder="unlimited"
            />
          </div>
          <div className="wf-config-field">
            <label className="wf-config-label">Sub-workflow Source</label>
            <div className="wf-map-mode-toggle">
              <button
                className={`wf-map-mode-btn${mapMode === 'workflow' ? ' wf-map-mode-btn--active' : ''}`}
                onClick={() => switchMapMode('workflow')}
                type="button"
              >
                Saved Workflow
              </button>
              <button
                className={`wf-map-mode-btn${mapMode === 'inline' ? ' wf-map-mode-btn--active' : ''}`}
                onClick={() => switchMapMode('inline')}
                type="button"
              >
                Inline Graph
              </button>
            </div>
          </div>
          {mapMode === 'workflow' && (
            <div className="wf-config-field">
              <div className="wf-config-hint">Select a saved workflow to run for each item</div>
              <select
                className="wf-config-input"
                value={(d.workflow_id as string) || ''}
                onChange={(e) => onUpdate({workflow_id: e.target.value || undefined})}
              >
                <option value="">— choose a workflow —</option>
                {savedWorkflows.map((wf) => (
                  <option key={wf.id} value={wf.id}>{wf.name}</option>
                ))}
              </select>
            </div>
          )}
          {mapMode === 'inline' && (
            <div className="wf-config-field">
              <div className="wf-config-hint">Paste &#123;"nodes":[], "edges":[]&#125;</div>
              <textarea
                className="wf-config-textarea"
                rows={5}
                value={(d.sub_graph as string) || ''}
                onChange={(e) => onUpdate({sub_graph: e.target.value})}
                placeholder='{"nodes": [], "edges": []}'
              />
            </div>
          )}
        </>
      )}

      <button className="wf-delete-node-btn" onClick={onDelete}>
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
  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes as never[]);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const {screenToFlowPosition} = useReactFlow();
  const {data: catalog} = useModels();
  const models = catalog?.available.map((m) => m.id) ?? [];

  // Sync initialNodes/initialEdges when workflow loads (handles page reload)
  useEffect(() => {
    setNodes(initialNodes as never[]);
    setEdges(initialEdges);
  }, [initialNodes.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // Overlay execution statuses onto node data
  useEffect(() => {
    if (Object.keys(nodeStatuses).length === 0) return;
    setNodes((ns) =>
      ns.map((n) => {
        const st = nodeStatuses[n.id];
        if (!st) return n;
        return {
          ...n,
          data: {
            ...(n.data as object),
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
      const defaultData: WorkflowNodeData =
        type === 'start'
          ? {label: 'Start', initial_inputs: {}}
          : type === 'agent'
            ? {label: 'New Agent'}
            : type === 'map'
              ? {label: 'Map', items_key: 'items', result_key: 'results'}
              : {label: 'New Condition'};
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
    setNodes((ns) =>
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

  return (
    <div className="wf-editor-inner">
      {/* Palette */}
      <div className="wf-palette">
        <div className="wf-palette-label">Nodes</div>
        <div
          className="wf-palette-item wf-palette-item--start"
          draggable
          onDragStart={(e) => e.dataTransfer.setData('nodeType', 'start')}
        >
          <strong>Start</strong>
          <div style={{fontSize: '0.65rem', marginTop: 1, opacity: 0.65}}>entry point</div>
        </div>
        <div
          className="wf-palette-item wf-palette-item--agent"
          draggable
          onDragStart={(e) => e.dataTransfer.setData('nodeType', 'agent')}
        >
          <strong>Agent</strong>
          <div style={{fontSize: '0.65rem', marginTop: 1, opacity: 0.65}}>LLM + tools</div>
        </div>
        <div
          className="wf-palette-item wf-palette-item--conditional"
          draggable
          onDragStart={(e) => e.dataTransfer.setData('nodeType', 'conditional')}
        >
          <strong>Conditional</strong>
          <div style={{fontSize: '0.65rem', marginTop: 1, opacity: 0.65}}>true / false branch</div>
        </div>
        <div
          className="wf-palette-item wf-palette-item--map"
          draggable
          onDragStart={(e) => e.dataTransfer.setData('nodeType', 'map')}
        >
          <strong>Map</strong>
          <div style={{fontSize: '0.65rem', marginTop: 1, opacity: 0.65}}>run sub-workflow per item</div>
        </div>
        <div className="wf-palette-hint">Drag nodes onto the canvas</div>
      </div>

      {/* Canvas */}
      <div className="wf-canvas-wrap" onDrop={onDrop} onDragOver={onDragOver}>
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
          <MiniMap nodeColor={() => 'var(--surface2)'} maskColor="rgba(0,0,0,0.4)" />
        </ReactFlow>
      </div>

      {/* Config panel */}
      {selectedNode && (
        <ConfigPanel
          node={selectedNode}
          models={models}
          onUpdate={updateNodeData}
          onDelete={deleteSelectedNode}
        />
      )}

      {/* Floating save/run bar */}
      <div className="wf-canvas-toolbar">
        <button className="wf-save-btn" disabled={saving} onClick={handleSave}>
          {saving ? 'Saving…' : 'Save'}
        </button>
        <button
          className="wf-run-btn"
          disabled={running}
          onClick={() => onRun(nodes as WorkflowRFNode[], edges as WorkflowRFEdge[])}
        >
          {running ? '⏳ Running' : '▶ Run'}
        </button>
      </div>
    </div>
  );
}
