import {ReactFlowProvider} from '@xyflow/react';
import {useNavigate, useParams} from '@tanstack/react-router';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {WorkflowDetailQuery as TWorkflowDetailQuery} from '../__generated__/WorkflowDetailQuery.graphql';
import type {WorkflowRunsQuery as TWorkflowRunsQuery} from '../__generated__/WorkflowRunsQuery.graphql';
import {QueryBoundary, useQueryRetry} from './QueryBoundary';
import {
  NotificationsEditor,
  parseNotifications,
  serializeNotifications,
} from './NotificationsEditor';
import {WorkflowEditor} from './WorkflowEditor';
import {useWorkflowRunEvents} from '../hooks/useWorkflowRunEvents';
import {formatRelativeTime} from '../lib/api';
import {commitRunWorkflow} from '../relay/RunWorkflowMutation';
import {commitUpdateWorkflow} from '../relay/UpdateWorkflowMutation';
import {
  refreshWorkflow,
  workflowDetailQuery,
  workflowDetailVars,
} from '../relay/WorkflowDetailQuery';
import {mapWorkflow} from '../relay/WorkflowListQuery';
import {
  mapWorkflowRun,
  workflowRunsQuery,
  workflowRunsVars,
} from '../relay/WorkflowRunsQuery';
import {parseDefinition, serializeDefinition} from '../lib/types';
import type {NotificationConfig, Workflow, WorkflowRFEdge, WorkflowRFNode} from '../lib/types';

// ── History panel ─────────────────────────────────────────────────────────────

function HistoryPanel(props: {workflowId: string; onClose: () => void}) {
  return (
    <QueryBoundary
      label="Failed to load run history"
      fallback={<div className="wf-history-sidebar" />}
    >
      <HistoryPanelInner {...props} />
    </QueryBoundary>
  );
}

function HistoryPanelInner({workflowId, onClose}: {workflowId: string; onClose: () => void}) {
  const navigate = useNavigate();
  const data = useLazyLoadQuery<TWorkflowRunsQuery>(
    workflowRunsQuery,
    workflowRunsVars(workflowId),
    {fetchPolicy: 'store-and-network'},
  );
  const runs = useMemo(() => data.workflowRuns.map(mapWorkflowRun), [data.workflowRuns]);

  return (
    <div className="wf-history-sidebar">
      <div className="wf-history-header">
        <span className="wf-modal-title">Run History</span>
        <button className="wf-history-close" onClick={onClose}>
          ✕
        </button>
      </div>

      <div style={{overflowY: 'auto', flex: 1}}>
        {runs.length === 0 && <div className="wf-history-empty">No runs yet.</div>}

        {runs.map((run) => {
          const duration = run.finished_at
            ? `${((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000).toFixed(1)}s`
            : null;
          const statusClass = `run-status--${run.status}`;

          let nodeCount = 0;
          try {
            if (run.node_results) nodeCount = (JSON.parse(run.node_results) as unknown[]).length;
          } catch { /* ignore */ }

          return (
            <div
              key={run.id}
              className="wf-history-row"
              style={{cursor: 'pointer'}}
              onClick={() =>
                void navigate({
                  to: '/workflow/$id/runs/$runId',
                  params: {id: workflowId, runId: run.id},
                })
              }
            >
              <div className="wf-history-row-header">
                <span className={`run-status ${statusClass}`}>{run.status}</span>
                <span className="wf-history-time">{formatRelativeTime(run.started_at)}</span>
                {duration && <span className="wf-history-duration">{duration}</span>}
                {nodeCount > 0 && (
                  <span className="wf-history-node-count">
                    {nodeCount} node{nodeCount !== 1 ? 's' : ''}
                  </span>
                )}
                <span className="wf-history-chevron">›</span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ── Run input modal ───────────────────────────────────────────────────────────

interface RunInputModalProps {
  requiredKeys: string[];
  defaultValues: Record<string, string>;
  onSubmit: (inputs: Record<string, string>) => void;
  onCancel: () => void;
}

function RunInputModal({requiredKeys, defaultValues, onSubmit, onCancel}: RunInputModalProps) {
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(requiredKeys.map((k) => [k, defaultValues[k] ?? ''])),
  );

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    onSubmit(values);
  }

  return (
    <div className="wf-modal-backdrop" onClick={onCancel}>
      <div className="wf-modal" onClick={(e) => e.stopPropagation()}>
        <div className="wf-modal-title">Run Inputs</div>
        <form onSubmit={handleSubmit} style={{display: 'flex', flexDirection: 'column', gap: 10}}>
          {requiredKeys.map((key) => (
            <div key={key} className="wf-config-field">
              <label className="wf-config-label">{key}</label>
              <input
                className="wf-config-input"
                value={values[key] ?? ''}
                onChange={(e) => setValues((v) => ({...v, [key]: e.target.value}))}
                autoFocus={key === requiredKeys[0]}
              />
            </div>
          ))}
          <div className="wf-modal-actions">
            <button type="button" className="wf-save-btn" onClick={onCancel}>
              Cancel
            </button>
            <button type="submit" className="wf-run-btn">
              Run
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Node status row ───────────────────────────────────────────────────────────

interface NodeStatusRowProps {
  nodeId: string;
  status: {
    status: string;
    label?: string;
    tokens?: string;
    verdict?: string;
    error?: string;
    output?: Record<string, unknown>;
  };
}

function NodeStatusRow({nodeId, status}: NodeStatusRowProps) {
  const statusClass =
    status.status === 'running'
      ? 'run-status--running'
      : status.status === 'done'
        ? 'run-status--done'
        : 'run-status--error';

  return (
    <div className="wf-node-status-row">
      <div className="wf-node-status-row-header">
        <span className={`run-status ${statusClass}`}>{status.status}</span>
        <span style={{color: 'var(--text)', fontSize: '0.75rem'}}>{status.label ?? nodeId}</span>
        {status.verdict && (
          <span
            style={{
              fontSize: '0.68rem',
              background: status.verdict === 'true' ? '#1e3a22' : '#3a1e1e',
              color: status.verdict === 'true' ? '#6bcf7f' : '#cf6b6b',
              borderRadius: 4,
              padding: '1px 6px',
            }}
          >
            {status.verdict}
          </span>
        )}
      </div>
      {status.tokens && <div className="wf-node-tokens">{status.tokens}</div>}
      {status.error && (
        <div style={{color: 'var(--error-text)', fontSize: '0.72rem', marginTop: 2}}>
          {status.error}
        </div>
      )}
    </div>
  );
}

// ── Settings modal ────────────────────────────────────────────────────────────

interface SettingsModalProps {
  workflow: Workflow;
  onClose: () => void;
  onSaved: () => void;
}

function SettingsModal({workflow, onClose, onSaved}: SettingsModalProps) {
  const [name, setName] = useState(workflow.name);
  const [description, setDescription] = useState(workflow.description ?? '');
  const [notifications, setNotifications] = useState<NotificationConfig[]>(
    parseNotifications(workflow.notifications),
  );
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      await commitUpdateWorkflow(workflow.id, {
        name,
        description: description || null,
        notifications: serializeNotifications(notifications),
      });
      onSaved();
      onClose();
    } catch (e) {
      setError((e as Error).message);
      setSaving(false);
    }
  }

  return (
    <div className="wf-modal-backdrop" onClick={onClose}>
      <div
        className="wf-modal"
        onClick={(e) => e.stopPropagation()}
        style={{minWidth: 480, maxWidth: 640}}
      >
        <div className="wf-modal-title">Workflow Settings</div>
        <div style={{display: 'flex', flexDirection: 'column', gap: 10}}>
          <div className="auto-form-group">
            <label className="auto-form-label">Name</label>
            <input
              className="auto-form-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={saving}
            />
          </div>
          <div className="auto-form-group">
            <label className="auto-form-label">Description</label>
            <input
              className="auto-form-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              disabled={saving}
            />
          </div>
          <NotificationsEditor
            value={notifications}
            onChange={setNotifications}
            disabled={saving}
          />
          {error && <div className="error-bubble">{error}</div>}
          <div className="wf-modal-actions">
            <button
              type="button"
              className="wf-save-btn"
              onClick={onClose}
              disabled={saving}
            >
              Cancel
            </button>
            <button
              type="button"
              className="wf-run-btn"
              onClick={() => void handleSave()}
              disabled={saving || !name.trim()}
            >
              {saving ? 'Saving…' : 'Save'}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function WorkflowEditorPage() {
  const {id} = useParams({from: '/workflow/$id/'});
  const navigate = useNavigate();

  const data = useLazyLoadQuery<TWorkflowDetailQuery>(
    workflowDetailQuery,
    workflowDetailVars(id),
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const workflow = useMemo(
    () => (data.workflow ? mapWorkflow(data.workflow) : null),
    [data.workflow],
  );

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [showRunPanel, setShowRunPanel] = useState(false);
  const [showInputModal, setShowInputModal] = useState(false);
  const [showHistory, setShowHistory] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [pendingNodes, setPendingNodes] = useState<WorkflowRFNode[]>([]);
  const [pendingEdges, setPendingEdges] = useState<WorkflowRFEdge[]>([]);
  const [pendingInputDefs, setPendingInputDefs] = useState<Record<string, string>>({});

  const {streaming, nodeStatuses, outputs, error: streamError} = useWorkflowRunEvents(
    activeRunId,
    id,
  );

  if (!workflow) {
    return <div style={{padding: 24, color: 'var(--text-dim)'}}>Workflow not found.</div>;
  }

  const {nodes: initNodes, edges: initEdges} = parseDefinition(workflow.definition);

  async function handleSave(nodes: WorkflowRFNode[], edges: WorkflowRFEdge[]) {
    setSaving(true);
    setSaveError(null);
    try {
      await commitUpdateWorkflow(id, {definition: serializeDefinition(nodes, edges)});
      await refreshWorkflow(id);
    } catch (e) {
      setSaveError((e as Error).message);
    } finally {
      setSaving(false);
    }
  }

  async function handleRunClick(nodes: WorkflowRFNode[], edges: WorkflowRFEdge[]) {
    await handleSave(nodes, edges);
    const targetIds = new Set(edges.map((e) => e.target));
    const entryNodes = nodes.filter((n) => !targetIds.has(n.id));

    const inputDefs: Record<string, string> = {};
    for (const n of entryNodes) {
      if (n.type === 'start' && n.data.initial_inputs) {
        Object.assign(inputDefs, n.data.initial_inputs as Record<string, string>);
      } else if (n.type === 'agent' && n.data.prompt_template) {
        const vars = [...(n.data.prompt_template as string).matchAll(/\{\{(.+?)\}\}/g)].map(
          (m) => m[1].trim(),
        );
        for (const key of vars) inputDefs[key] = '';
      }
    }

    setPendingNodes(nodes);
    setPendingEdges(edges);
    setPendingInputDefs(inputDefs);

    if (Object.keys(inputDefs).length > 0) {
      setShowInputModal(true);
    } else {
      void doTriggerRun({});
    }
  }

  async function doTriggerRun(inputs: Record<string, string>) {
    setShowInputModal(false);
    try {
      const {run_id} = await commitRunWorkflow(id, inputs);
      setActiveRunId(run_id);
      setShowRunPanel(true);
    } catch (e) {
      setSaveError((e as Error).message);
    }
  }

  const runStatusLabel = streaming ? 'Running…' : streamError ? 'Error' : 'Done';
  const runStatusClass = streaming
    ? 'run-status--running'
    : streamError
      ? 'run-status--error'
      : 'run-status--done';

  // suppress unused variable warning
  void pendingNodes;
  void pendingEdges;

  return (
    <div className="wf-editor-page">
      {/* Top bar */}
      <div className="wf-editor-topbar">
        <button className="wf-back-btn" onClick={() => void navigate({to: '/workflow'})}>
          ← Workflows
        </button>
        <span className="wf-workflow-name">{workflow.name}</span>
        {saveError && (
          <span style={{fontSize: '0.72rem', color: 'var(--error-text)'}}>{saveError}</span>
        )}
        <button
          className="wf-save-btn"
          onClick={() => setShowSettings(true)}
        >
          Settings
        </button>
        <button
          className={`wf-save-btn${showHistory ? ' wf-save-btn--active' : ''}`}
          onClick={() => setShowHistory((v) => !v)}
        >
          History
        </button>
      </div>

      {/* Editor body */}
      <div className="wf-editor-body">
        <ReactFlowProvider>
          <WorkflowEditor
            initialNodes={initNodes}
            initialEdges={initEdges}
            nodeStatuses={nodeStatuses}
            onSave={handleSave}
            onRun={(nodes, edges) => void handleRunClick(nodes, edges)}
            saving={saving}
            running={streaming}
          />
        </ReactFlowProvider>
        {showHistory && <HistoryPanel workflowId={id} onClose={() => setShowHistory(false)} />}
      </div>

      {/* Run panel */}
      {showRunPanel && (
        <div className="wf-run-panel">
          <div className="wf-run-panel-header">
            <span className={`run-status ${runStatusClass}`}>{runStatusLabel}</span>
            <span style={{fontSize: '0.72rem', color: 'var(--text-dim)', flex: 1, marginLeft: 8}}>
              {activeRunId}
            </span>
            <button
              style={{
                background: 'none',
                border: 'none',
                color: 'var(--text-dim)',
                cursor: 'pointer',
                fontSize: '0.88rem',
              }}
              onClick={() => setShowRunPanel(false)}
            >
              ✕
            </button>
          </div>
          {Object.entries(nodeStatuses).map(([nodeId, ns]) => (
            <NodeStatusRow key={nodeId} nodeId={nodeId} status={ns} />
          ))}
          {streamError && (
            <div style={{padding: '8px 16px', color: 'var(--error-text)', fontSize: '0.78rem'}}>
              {streamError}
            </div>
          )}
          {outputs && !streaming && (
            <div className="wf-run-outputs">{JSON.stringify(outputs, null, 2)}</div>
          )}
        </div>
      )}

      {/* Run input modal */}
      {showInputModal && (
        <RunInputModal
          requiredKeys={Object.keys(pendingInputDefs)}
          defaultValues={pendingInputDefs}
          onSubmit={(inputs) => void doTriggerRun(inputs)}
          onCancel={() => setShowInputModal(false)}
        />
      )}

      {/* Settings modal */}
      {showSettings && (
        <SettingsModal
          workflow={workflow}
          onClose={() => setShowSettings(false)}
          onSaved={() => void refreshWorkflow(id)}
        />
      )}
    </div>
  );
}
