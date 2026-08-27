import '@xyflow/react/dist/style.css';
import * as stylex from '@stylexjs/stylex';
import {useNavigate, useParams} from '@tanstack/react-router';
import {Background, Controls, MiniMap, ReactFlow, ReactFlowProvider} from '@xyflow/react';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {WorkflowDetailQuery as TWorkflowDetailQuery} from '../__generated__/WorkflowDetailQuery.graphql';
import type {WorkflowRunDetailQuery as TWorkflowRunDetailQuery} from '../__generated__/WorkflowRunDetailQuery.graphql';
import {parseDefinition} from '../lib/types';
import type {NodeRecord} from '../lib/types';
import {workflowDetailQuery, workflowDetailVars} from '../relay/WorkflowDetailQuery';
import {mapWorkflow} from '../relay/WorkflowListQuery';
import {workflowRunDetailQuery, workflowRunDetailVars} from '../relay/WorkflowRunDetailQuery';
import {mapWorkflowRun} from '../relay/WorkflowRunsQuery';
import {colors} from '../theme/tokens.stylex';
import {useQueryRetry} from './QueryBoundary';
import {
  editor,
  history,
  runDetail,
  runStatus,
  statusStyle,
  typeBadgeStyle,
  wfBtn,
} from './workflow.styles';
import {nodeTypes} from './WorkflowEditor';

// ── Node detail panel ─────────────────────────────────────────────────────────

function RunNodeDetail({rec}: {rec: NodeRecord}) {
  const duration = rec.finished_at
    ? `${((new Date(rec.finished_at).getTime() - new Date(rec.started_at).getTime()) / 1000).toFixed(1)}s`
    : null;

  const plan = (() => {
    if (rec.node_type !== 'planner' && rec.node_type !== 'plan') return null;
    const out = rec.outputs || {};
    const raw = (out as any).plan || (out as any).result || [];
    if (Array.isArray(raw)) return raw as string[];
    return null;
  })();

  const isPlanner = rec.node_type === 'planner' || rec.node_type === 'plan';

  return (
    <div {...stylex.props(runDetail.panel)}>
      <div {...stylex.props(runDetail.header)}>
        <span {...stylex.props(history.badge, typeBadgeStyle(rec.node_type))}>{rec.node_type}</span>
        <span {...stylex.props(styles.recordLabel)}>{rec.label}</span>
        <span {...stylex.props(runStatus.base, statusStyle(rec.status))}>{rec.status}</span>
        {duration && <span {...stylex.props(styles.duration)}>{duration}</span>}
      </div>

      {rec.verdict && (
        <div {...stylex.props(styles.verdictRow)}>
          <span
            {...stylex.props(
              history.verdict,
              rec.verdict === 'true' ? history.verdictTrue : history.verdictFalse,
            )}
          >
            {rec.verdict}
          </span>
        </div>
      )}

      {rec.error && <div {...stylex.props(runDetail.error)}>{rec.error}</div>}

      {isPlanner && plan && plan.length > 0 && (
        <div {...stylex.props(runDetail.section)}>
          <div {...stylex.props(runDetail.label)}>Plan ({plan.length} steps)</div>
          <ol {...stylex.props(styles.planList)}>
            {plan.map((s: string, i: number) => (
              <li key={i} {...stylex.props(styles.planItem)}>
                {s}
              </li>
            ))}
          </ol>
        </div>
      )}

      {rec.rendered_prompt && !isPlanner && (
        <div {...stylex.props(runDetail.section)}>
          <div {...stylex.props(runDetail.label)}>Prompt</div>
          <pre {...stylex.props(runDetail.pre)}>{rec.rendered_prompt}</pre>
        </div>
      )}

      {rec.node_type !== 'start' && !isPlanner && (
        <div {...stylex.props(runDetail.section)}>
          <div {...stylex.props(runDetail.label)}>In</div>
          <pre {...stylex.props(runDetail.pre)}>
            {Object.keys(rec.inputs).length > 0 ? JSON.stringify(rec.inputs, null, 2) : '—'}
          </pre>
        </div>
      )}

      {rec.node_type !== 'conditional' && !isPlanner && (
        <div {...stylex.props(runDetail.section)}>
          <div {...stylex.props(runDetail.label)}>Out</div>
          <pre {...stylex.props(runDetail.pre)}>
            {rec.outputs && Object.keys(rec.outputs).length > 0
              ? JSON.stringify(rec.outputs, null, 2)
              : '—'}
          </pre>
        </div>
      )}

      {isPlanner && rec.outputs && (
        <div {...stylex.props(runDetail.section)}>
          <div {...stylex.props(runDetail.label)}>Raw Outputs</div>
          <pre {...stylex.props(runDetail.pre)}>{JSON.stringify(rec.outputs, null, 2)}</pre>
        </div>
      )}
    </div>
  );
}

// ── All-nodes summary (default / no selection) ───────────────────────────────

function AllNodesSummary({nodeRecordMap}: {nodeRecordMap: Map<string, NodeRecord>}) {
  const records = Array.from(nodeRecordMap.values());

  if (records.length === 0) {
    return <div {...stylex.props(runDetail.empty)}>No execution data for this run.</div>;
  }

  return (
    <div {...stylex.props(runDetail.panel)}>
      <div {...stylex.props(runDetail.header)}>
        <span {...stylex.props(styles.summaryTitle)}>All Nodes</span>
        <span {...stylex.props(styles.summaryCount)}>
          {records.length} node{records.length !== 1 ? 's' : ''}
        </span>
      </div>
      {records.map((rec) => {
        const duration = rec.finished_at
          ? `${((new Date(rec.finished_at).getTime() - new Date(rec.started_at).getTime()) / 1000).toFixed(1)}s`
          : null;
        return (
          <div key={rec.node_id} {...stylex.props(runDetail.summaryRow)}>
            <span {...stylex.props(history.badge, typeBadgeStyle(rec.node_type))}>
              {rec.node_type}
            </span>
            <span {...stylex.props(runDetail.summaryLabel)}>{rec.label}</span>
            <span {...stylex.props(runStatus.base, statusStyle(rec.status))}>{rec.status}</span>
            {rec.verdict && (
              <span
                {...stylex.props(
                  history.verdict,
                  rec.verdict === 'true' ? history.verdictTrue : history.verdictFalse,
                )}
              >
                {rec.verdict}
              </span>
            )}
            {duration && <span {...stylex.props(styles.summaryDuration)}>{duration}</span>}
          </div>
        );
      })}
    </div>
  );
}

// ── Page ──────────────────────────────────────────────────────────────────────

export default function WorkflowRunPage() {
  const {id, runId} = useParams({from: '/workflow/$id/runs/$runId'});
  const navigate = useNavigate();

  const workflowData = useLazyLoadQuery<TWorkflowDetailQuery>(
    workflowDetailQuery,
    workflowDetailVars(id),
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const workflow = workflowData.workflow ? mapWorkflow(workflowData.workflow) : null;

  const runData = useLazyLoadQuery<TWorkflowRunDetailQuery>(
    workflowRunDetailQuery,
    workflowRunDetailVars(runId),
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const run = runData.workflowRun ? mapWorkflowRun(runData.workflowRun) : null;

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
    <div {...stylex.props(editor.page)}>
      {/* Top bar */}
      <div {...stylex.props(editor.topbar)}>
        <button
          {...stylex.props(wfBtn.back)}
          onClick={() => void navigate({to: '/workflow/$id', params: {id}})}
        >
          ← Editor
        </button>
        <span {...stylex.props(editor.name)}>{workflow?.name}</span>
        {run && (
          <span {...stylex.props(runStatus.base, statusStyle(run.status))}>{run.status}</span>
        )}
        <span {...stylex.props(styles.duration)}>{runId.slice(0, 8)}</span>
      </div>

      {/* Canvas + detail panel */}
      <div {...stylex.props(editor.body)}>
        <ReactFlowProvider>
          {/* `editor.canvas` also publishes the --rf-* palette base.css reads. */}
          <div {...stylex.props(editor.canvas)}>
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
              <MiniMap nodeColor={() => 'var(--rf-surface2)'} maskColor="rgba(0,0,0,0.4)" />
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

const styles = stylex.create({
  recordLabel: {flex: 1, fontSize: '0.88rem', fontWeight: 600},
  duration: {fontSize: '0.72rem', color: colors.textDim},
  verdictRow: {paddingBlock: 8, paddingInline: 16},

  planList: {margin: 0, paddingInlineStart: 20, fontSize: '0.86rem', lineHeight: 1.6},
  planItem: {marginBlockEnd: 2},

  summaryTitle: {fontSize: '0.75rem', fontWeight: 600, color: colors.text, flex: 1},
  summaryCount: {fontSize: '0.68rem', color: colors.textDim},
  summaryDuration: {fontSize: '0.68rem', color: colors.textDim, marginInlineStart: 'auto'},
});
