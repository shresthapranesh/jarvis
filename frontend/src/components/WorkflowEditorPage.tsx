import * as stylex from '@stylexjs/stylex';
import {useNavigate, useParams} from '@tanstack/react-router';
import {ReactFlowProvider} from '@xyflow/react';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {WorkflowDetailQuery as TWorkflowDetailQuery} from '../__generated__/WorkflowDetailQuery.graphql';
import type {WorkflowRunsQuery as TWorkflowRunsQuery} from '../__generated__/WorkflowRunsQuery.graphql';
import {useWorkflowRunEvents} from '../hooks/useWorkflowRunEvents';
import {formatRelativeTime} from '../lib/api';
import {parseDefinition, serializeDefinition} from '../lib/types';
import type {NotificationConfig, Workflow, WorkflowRFEdge, WorkflowRFNode} from '../lib/types';
import {commitRunWorkflow} from '../relay/RunWorkflowMutation';
import {commitUpdateWorkflow} from '../relay/UpdateWorkflowMutation';
import {
  refreshWorkflow,
  workflowDetailQuery,
  workflowDetailVars,
} from '../relay/WorkflowDetailQuery';
import {mapWorkflow} from '../relay/WorkflowListQuery';
import {mapWorkflowRun, workflowRunsQuery, workflowRunsVars} from '../relay/WorkflowRunsQuery';
import {colors} from '../theme/tokens.stylex';
import {
  NotificationsEditor,
  parseNotifications,
  serializeNotifications,
} from './NotificationsEditor';
import {QueryBoundary, useQueryRetry} from './QueryBoundary';
import {errorBubble, field} from './ui';
import {
  config,
  editor,
  history,
  modal,
  runPanel,
  runStatus,
  statusStyle,
  wfBtn,
} from './workflow.styles';
import {WorkflowEditor} from './WorkflowEditor';

// ── History panel ─────────────────────────────────────────────────────────────

function HistoryPanel(props: {workflowId: string; onClose: () => void}) {
  return (
    <QueryBoundary
      label="Failed to load run history"
      fallback={<div {...stylex.props(history.sidebar)} />}
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
    <div {...stylex.props(history.sidebar)}>
      <div {...stylex.props(history.header)}>
        <span {...stylex.props(modal.title)}>Run History</span>
        <button {...stylex.props(history.close)} onClick={onClose}>
          ✕
        </button>
      </div>

      <div {...stylex.props(history.scroll)}>
        {runs.length === 0 && <div {...stylex.props(history.empty)}>No runs yet.</div>}

        {runs.map((run) => {
          const duration = run.finished_at
            ? `${((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000).toFixed(1)}s`
            : null;
          let nodeCount = 0;
          try {
            if (run.node_results) nodeCount = (JSON.parse(run.node_results) as unknown[]).length;
          } catch {
            /* ignore */
          }

          return (
            <div
              key={run.id}
              {...stylex.props(history.row, styles.clickable)}
              onClick={() =>
                void navigate({
                  to: '/workflow/$id/runs/$runId',
                  params: {id: workflowId, runId: run.id},
                })
              }
            >
              <div {...stylex.props(history.rowHeader)}>
                <span {...stylex.props(runStatus.base, statusStyle(run.status))}>{run.status}</span>
                <span {...stylex.props(history.time)}>{formatRelativeTime(run.started_at)}</span>
                {duration && <span {...stylex.props(history.duration)}>{duration}</span>}
                {nodeCount > 0 && (
                  <span {...stylex.props(history.nodeCount)}>
                    {nodeCount} node{nodeCount !== 1 ? 's' : ''}
                  </span>
                )}
                <span {...stylex.props(history.chevron)}>›</span>
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
    <div {...stylex.props(modal.backdrop)} onClick={onCancel}>
      <div {...stylex.props(modal.root)} onClick={(e) => e.stopPropagation()}>
        <div {...stylex.props(modal.title)}>Run Inputs</div>
        <form onSubmit={handleSubmit} {...stylex.props(styles.stack)}>
          {requiredKeys.map((key) => (
            <div key={key} {...stylex.props(config.field)}>
              <label {...stylex.props(config.label)}>{key}</label>
              <input
                {...stylex.props(config.input)}
                value={values[key] ?? ''}
                onChange={(e) => setValues((v) => ({...v, [key]: e.target.value}))}
                autoFocus={key === requiredKeys[0]}
              />
            </div>
          ))}
          <div {...stylex.props(modal.actions)}>
            <button type="button" {...stylex.props(wfBtn.save)} onClick={onCancel}>
              Cancel
            </button>
            <button type="submit" {...stylex.props(wfBtn.run)}>
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
  // Anything that is not running or done reads as a failure here, which is
  // what the original three-way ternary did.
  const pill = status.status === 'running' || status.status === 'done' ? status.status : 'error';

  return (
    <div {...stylex.props(runPanel.statusRow)}>
      <div {...stylex.props(runPanel.statusHead)}>
        <span {...stylex.props(runStatus.base, statusStyle(pill))}>{status.status}</span>
        <span {...stylex.props(styles.nodeLabel)}>{status.label ?? nodeId}</span>
        {status.verdict && (
          <span
            {...stylex.props(
              styles.verdict,
              status.verdict === 'true' ? styles.verdictTrue : styles.verdictFalse,
            )}
          >
            {status.verdict}
          </span>
        )}
      </div>
      {status.tokens && <div {...stylex.props(runPanel.tokens)}>{status.tokens}</div>}
      {status.error && <div {...stylex.props(styles.nodeError)}>{status.error}</div>}
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
    <div {...stylex.props(modal.backdrop)} onClick={onClose}>
      <div {...stylex.props(modal.root, styles.settingsModal)} onClick={(e) => e.stopPropagation()}>
        <div {...stylex.props(modal.title)}>Workflow Settings</div>
        <div {...stylex.props(styles.stack)}>
          <div {...stylex.props(field.group)}>
            <label {...stylex.props(field.label)}>Name</label>
            <input
              {...stylex.props(field.input)}
              value={name}
              onChange={(e) => setName(e.target.value)}
              disabled={saving}
            />
          </div>
          <div {...stylex.props(field.group)}>
            <label {...stylex.props(field.label)}>Description</label>
            <input
              {...stylex.props(field.input)}
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
          {error && <div {...stylex.props(errorBubble.base)}>{error}</div>}
          <div {...stylex.props(modal.actions)}>
            <button type="button" {...stylex.props(wfBtn.save)} onClick={onClose} disabled={saving}>
              Cancel
            </button>
            <button
              type="button"
              {...stylex.props(wfBtn.run)}
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

  const data = useLazyLoadQuery<TWorkflowDetailQuery>(workflowDetailQuery, workflowDetailVars(id), {
    fetchPolicy: 'store-and-network',
    fetchKey: useQueryRetry(),
  });
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

  const {
    streaming,
    nodeStatuses,
    outputs,
    error: streamError,
  } = useWorkflowRunEvents(activeRunId, id);

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
        const vars = [...(n.data.prompt_template as string).matchAll(/\{\{(.+?)\}\}/g)].map((m) =>
          m[1].trim(),
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
  const runStatusKind = streaming ? 'running' : streamError ? 'error' : 'done';

  // suppress unused variable warning
  void pendingNodes;
  void pendingEdges;

  return (
    <div {...stylex.props(editor.page)}>
      {/* Top bar */}
      <div {...stylex.props(editor.topbar)}>
        <button {...stylex.props(wfBtn.back)} onClick={() => void navigate({to: '/workflow'})}>
          ← Workflows
        </button>
        <span {...stylex.props(editor.name)}>{workflow.name}</span>
        {saveError && <span {...stylex.props(styles.saveError)}>{saveError}</span>}
        <button {...stylex.props(wfBtn.save)} onClick={() => setShowSettings(true)}>
          Settings
        </button>
        <button
          {...stylex.props(wfBtn.save, showHistory && wfBtn.saveActive)}
          onClick={() => setShowHistory((v) => !v)}
        >
          History
        </button>
      </div>

      {/* Editor body */}
      <div {...stylex.props(editor.body)}>
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
        <div {...stylex.props(runPanel.root)}>
          <div {...stylex.props(runPanel.header)}>
            <span {...stylex.props(runStatus.base, statusStyle(runStatusKind))}>
              {runStatusLabel}
            </span>
            <span {...stylex.props(styles.runId)}>{activeRunId}</span>
            <button {...stylex.props(history.close)} onClick={() => setShowRunPanel(false)}>
              ✕
            </button>
          </div>
          {Object.entries(nodeStatuses).map(([nodeId, ns]) => (
            <NodeStatusRow key={nodeId} nodeId={nodeId} status={ns} />
          ))}
          {streamError && <div {...stylex.props(styles.runError)}>{streamError}</div>}
          {outputs && !streaming && (
            <div {...stylex.props(runPanel.outputs)}>{JSON.stringify(outputs, null, 2)}</div>
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

const styles = stylex.create({
  /** The vertical field stack both modals wrap their body in. */
  stack: {display: 'flex', flexDirection: 'column', gap: 10},
  /** Settings holds more than a confirmation, so it overrides the modal width. */
  settingsModal: {minWidth: 480, maxWidth: 640},

  clickable: {cursor: 'pointer'},
  saveError: {fontSize: '0.72rem', color: colors.errorText},
  runId: {fontSize: '0.72rem', color: colors.textDim, flex: 1, marginInlineStart: 8},
  runError: {paddingBlock: 8, paddingInline: 16, color: colors.errorText, fontSize: '0.78rem'},

  nodeLabel: {color: colors.text, fontSize: '0.75rem'},
  nodeError: {color: colors.errorText, fontSize: '0.72rem', marginBlockStart: 2},
  verdict: {fontSize: '0.68rem', borderRadius: 4, paddingBlock: 1, paddingInline: 6},
  verdictTrue: {backgroundColor: '#1e3a22', color: '#6bcf7f'},
  verdictFalse: {backgroundColor: '#3a1e1e', color: '#cf6b6b'},
});
