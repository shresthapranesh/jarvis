import '@xyflow/react/dist/style.css';
import {
  Background,
  Controls,
  MiniMap,
  ReactFlow,
  ReactFlowProvider,
} from '@xyflow/react';
import {useQuery} from '@tanstack/react-query';
import {createFileRoute, useNavigate} from '@tanstack/react-router';
import {useMemo, useState} from 'react';
import {nodeTypes} from '../components/WorkflowEditor';
import {getWorkflow, getWorkflowRun} from '../lib/api';
import {parseDefinition} from '../lib/types';
import type {NodeRecord} from '../lib/types';

export const Route = createFileRoute('/workflow/$id/runs/$runId')({
  loader: ({context: {queryClient}, params: {runId}}) =>
    queryClient.ensureQueryData({
      queryKey: ['workflow-run', runId],
      queryFn: () => getWorkflowRun(runId),
    }),
  component: WorkflowRunPage,
});

// ── Node detail panel ─────────────────────────────────────────────────────────

function RunNodeDetail({rec}: {rec: NodeRecord}) {
  const duration = rec.finished_at
    ? `${((new Date(rec.finished_at).getTime() - new Date(rec.started_at).getTime()) / 1000).toFixed(1)}s`
    : null;

  return (
    <div className="wf-run-detail-panel">
      <div className="wf-run-detail-header">
        <span className={`wf-hist-type-badge wf-hist-type-badge--${rec.node_type}`}>
          {rec.node_type}
        </span>
        <span style={{flex: 1, fontSize: '0.88rem', fontWeight: 600}}>{rec.label}</span>
        <span className={`run-status run-status--${rec.status}`}>{rec.status}</span>
        {duration && (
          <span style={{fontSize: '0.72rem', color: 'var(--text-dim)'}}>{duration}</span>
        )}
      </div>

      {rec.verdict && (
        <div style={{padding: '8px 16px'}}>
          <span className={`wf-hist-verdict wf-hist-verdict--${rec.verdict}`}>{rec.verdict}</span>
        </div>
      )}

      {rec.error && <div className="wf-run-detail-error">{rec.error}</div>}

      {rec.rendered_prompt && (
        <div className="wf-run-detail-section">
          <div className="wf-run-detail-label">Prompt</div>
          <pre className="wf-run-detail-pre">{rec.rendered_prompt}</pre>
        </div>
      )}

      {rec.node_type !== 'start' && (
        <div className="wf-run-detail-section">
          <div className="wf-run-detail-label">In</div>
          <pre className="wf-run-detail-pre">
            {Object.keys(rec.inputs).length > 0 ? JSON.stringify(rec.inputs, null, 2) : '—'}
          </pre>
        </div>
      )}

      {rec.node_type !== 'conditional' && (
        <div className="wf-run-detail-section">
          <div className="wf-run-detail-label">Out</div>
          <pre className="wf-run-detail-pre">
            {rec.outputs && Object.keys(rec.outputs).length > 0
              ? JSON.stringify(rec.outputs, null, 2)
              : '—'}
          </pre>
        </div>
      )}
    </div>
  );
}

// ── All-nodes summary (default / no selection) ───────────────────────────────

function AllNodesSummary({nodeRecordMap}: {nodeRecordMap: Map<string, NodeRecord>}) {
  const records = Array.from(nodeRecordMap.values());

  if (records.length === 0) {
    return <div className="wf-run-detail-empty">No execution data for this run.</div>;
  }

  return (
    <div className="wf-run-detail-panel">
      <div className="wf-run-detail-header">
        <span style={{fontSize: '0.75rem', fontWeight: 600, color: 'var(--text)', flex: 1}}>
          All Nodes
        </span>
        <span style={{fontSize: '0.68rem', color: 'var(--text-dim)'}}>
          {records.length} node{records.length !== 1 ? 's' : ''}
        </span>
      </div>
      {records.map((rec) => {
        const duration = rec.finished_at
          ? `${((new Date(rec.finished_at).getTime() - new Date(rec.started_at).getTime()) / 1000).toFixed(1)}s`
          : null;
        return (
          <div key={rec.node_id} className="wf-run-summary-row">
            <span className={`wf-hist-type-badge wf-hist-type-badge--${rec.node_type}`}>
              {rec.node_type}
            </span>
            <span className="wf-run-summary-label">{rec.label}</span>
            <span className={`run-status run-status--${rec.status}`}>{rec.status}</span>
            {rec.verdict && (
              <span className={`wf-hist-verdict wf-hist-verdict--${rec.verdict}`}>
                {rec.verdict}
              </span>
            )}
            {duration && (
              <span style={{fontSize: '0.68rem', color: 'var(--text-dim)', marginLeft: 'auto'}}>
                {duration}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

function WorkflowRunPage() {
  const {id, runId} = Route.useParams();
  const navigate = useNavigate();

  const {data: workflow} = useQuery({
    queryKey: ['workflow', id],
    queryFn: () => getWorkflow(id),
    staleTime: 30_000,
  });

  const {data: run} = useQuery({
    queryKey: ['workflow-run', runId],
    queryFn: () => getWorkflowRun(runId),
    staleTime: 10_000,
  });

  const {nodes: baseNodes, edges} = parseDefinition(
    workflow?.definition ?? '{"nodes":[],"edges":[]}',
  );

  const nodeRecordMap = useMemo(() => {
    const map = new Map<string, NodeRecord>();
    try {
      const records: NodeRecord[] = JSON.parse(run?.node_results ?? '[]');
      for (const r of records) map.set(r.node_id, r);
    } catch {
      /* ignore */
    }
    return map;
  }, [run?.node_results]);

  const nodes = useMemo(
    () =>
      baseNodes.map((n) => {
        const rec = nodeRecordMap.get(n.id);
        if (!rec) return n;
        return {...n, data: {...n.data, _execStatus: rec.status}};
      }),
    [baseNodes, nodeRecordMap],
  );

  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const selectedRecord = selectedNodeId ? (nodeRecordMap.get(selectedNodeId) ?? null) : null;

  return (
    <div className="wf-editor-page">
      {/* Top bar */}
      <div className="wf-editor-topbar">
        <button
          className="wf-back-btn"
          onClick={() => void navigate({to: '/workflow/$id', params: {id}})}
        >
          ← Editor
        </button>
        <span className="wf-workflow-name">{workflow?.name}</span>
        {run && (
          <span className={`run-status run-status--${run.status}`}>{run.status}</span>
        )}
        <span style={{fontSize: '0.72rem', color: 'var(--text-dim)'}}>{runId.slice(0, 8)}</span>
      </div>

      {/* Canvas + detail panel */}
      <div className="wf-editor-body">
        <ReactFlowProvider>
          <div style={{flex: 1, position: 'relative', overflow: 'hidden'}}>
            <ReactFlow
              nodes={nodes}
              edges={edges}
              nodeTypes={nodeTypes}
              nodesDraggable={false}
              nodesConnectable={false}
              elementsSelectable={true}
              onNodeClick={(_, node) => setSelectedNodeId(node.id)}
              onPaneClick={() => setSelectedNodeId(null)}
              fitView
            >
              <Background />
              <Controls />
              <MiniMap nodeColor={() => 'var(--surface2)'} maskColor="rgba(0,0,0,0.4)" />
            </ReactFlow>
          </div>
        </ReactFlowProvider>

        {selectedRecord ? (
          <RunNodeDetail rec={selectedRecord} />
        ) : (
          <AllNodesSummary nodeRecordMap={nodeRecordMap} />
        )}
      </div>
    </div>
  );
}
