import * as stylex from '@stylexjs/stylex';
import {createFileRoute, useNavigate} from '@tanstack/react-router';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {WorkflowListQuery as TWorkflowListQuery} from '../__generated__/WorkflowListQuery.graphql';
import {QueryBoundary, useQueryRetry} from '../components/QueryBoundary';
import {config, list, modal, newBtn, wfBtn} from '../components/workflow.styles';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {formatRelativeTime} from '../lib/api';
import {commitCreateWorkflow} from '../relay/CreateWorkflowMutation';
import {commitDeleteWorkflow} from '../relay/DeleteWorkflowMutation';
import {mapWorkflow, refreshWorkflowList, workflowListQuery} from '../relay/WorkflowListQuery';

export const Route = createFileRoute('/workflow/')({component: WorkflowListRoute});

function WorkflowListRoute() {
  return (
    <QueryBoundary
      label="Failed to load workflows"
      fallback={<div {...stylex.props(list.empty)}>Loading…</div>}
    >
      <WorkflowListPage />
    </QueryBoundary>
  );
}

function WorkflowListPage() {
  const navigate = useNavigate();
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const data = useLazyLoadQuery<TWorkflowListQuery>(
    workflowListQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const workflows = useMemo(() => data.workflows.map(mapWorkflow), [data.workflows]);

  const deleteAction = useAsyncAction(
    async (id: string) => {
      await commitDeleteWorkflow(id);
      await refreshWorkflowList();
    },
    {onSuccess: () => setConfirmDeleteId(null)},
  );

  const createAction = useAsyncAction(
    async (input: {name: string; description: string | null; definition: string}) => {
      const wf = await commitCreateWorkflow(input);
      await refreshWorkflowList();
      void navigate({to: '/workflow/$id', params: {id: wf.id}});
    },
  );

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    void createAction.run({
      name: newName.trim(),
      description: newDesc.trim() || null,
      definition: '{"nodes":[],"edges":[]}',
    });
  }

  const confirmDeleteWorkflow = workflows.find((wf) => wf.id === confirmDeleteId);

  return (
    <div {...stylex.props(list.page)}>
      <div {...stylex.props(list.header)}>
        <h2 {...stylex.props(list.title)}>Workflows</h2>
        <button {...stylex.props(newBtn.base)} onClick={() => setShowCreate((v) => !v)}>
          {showCreate ? 'Cancel' : '+ New'}
        </button>
      </div>

      {showCreate && (
        <form {...stylex.props(list.createForm)} onSubmit={handleCreate}>
          <input
            {...stylex.props(config.input, list.createInput)}
            placeholder="Workflow name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            autoFocus
          />
          <input
            {...stylex.props(config.input, list.createInput)}
            placeholder="Description (optional)"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
          />
          <button
            type="submit"
            {...stylex.props(newBtn.base)}
            disabled={createAction.pending || !newName.trim()}
          >
            {createAction.pending ? 'Creating…' : 'Create'}
          </button>
        </form>
      )}

      {workflows.length === 0 && (
        <div {...stylex.props(list.empty)}>No workflows yet. Create one above.</div>
      )}

      {workflows.map((wf) => (
        <div
          key={wf.id}
          {...stylex.props(list.row)}
          onClick={() => void navigate({to: '/workflow/$id', params: {id: wf.id}})}
        >
          <div {...stylex.props(list.rowInfo)}>
            <div {...stylex.props(list.rowName)}>{wf.name}</div>
            {wf.description && <div {...stylex.props(list.rowDesc)}>{wf.description}</div>}
            <div {...stylex.props(list.rowMeta)}>{formatRelativeTime(wf.updated_at)}</div>
          </div>
          <div {...stylex.props(list.rowActions)}>
            <button
              {...stylex.props(wfBtn.del, wfBtn.delInline)}
              onClick={(e) => {
                e.stopPropagation();
                setConfirmDeleteId(wf.id);
              }}
            >
              Delete
            </button>
          </div>
        </div>
      ))}

      {/* Delete confirmation modal */}
      {confirmDeleteId && confirmDeleteWorkflow && (
        <div {...stylex.props(modal.backdrop)} onClick={() => setConfirmDeleteId(null)}>
          <div {...stylex.props(modal.root)} onClick={(e) => e.stopPropagation()}>
            <div {...stylex.props(modal.title)}>Delete workflow?</div>
            <p {...stylex.props(modal.body)}>
              <strong {...stylex.props(modal.strong)}>{confirmDeleteWorkflow.name}</strong> and all
              its run history will be permanently deleted.
            </p>
            <div {...stylex.props(modal.actions)}>
              <button {...stylex.props(wfBtn.save)} onClick={() => setConfirmDeleteId(null)}>
                Cancel
              </button>
              <button
                {...stylex.props(wfBtn.del, wfBtn.delInline)}
                disabled={deleteAction.pending}
                onClick={() => void deleteAction.run(confirmDeleteId)}
              >
                {deleteAction.pending ? 'Deleting…' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
