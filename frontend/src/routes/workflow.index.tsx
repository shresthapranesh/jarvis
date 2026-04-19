import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {createFileRoute, useNavigate} from '@tanstack/react-router';
import {useState} from 'react';
import {createWorkflow, deleteWorkflow, formatRelativeTime, listWorkflows} from '../lib/api';

export const Route = createFileRoute('/workflow/')({component: WorkflowListPage});

function WorkflowListPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [confirmDeleteId, setConfirmDeleteId] = useState<string | null>(null);

  const {data: workflows = [], isLoading} = useQuery({
    queryKey: ['workflows'],
    queryFn: listWorkflows,
    staleTime: 10_000,
  });

  const deleteMutation = useMutation({
    mutationFn: deleteWorkflow,
    onSuccess: () => {
      setConfirmDeleteId(null);
      queryClient.invalidateQueries({queryKey: ['workflows']});
    },
  });

  const createMutation = useMutation({
    mutationFn: createWorkflow,
    onSuccess: (wf) => {
      queryClient.invalidateQueries({queryKey: ['workflows']});
      void navigate({to: '/workflow/$id', params: {id: wf.id}});
    },
  });

  function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    createMutation.mutate({
      name: newName.trim(),
      description: newDesc.trim() || null,
      definition: '{"nodes":[],"edges":[]}',
    });
  }

  const confirmDeleteWorkflow = workflows.find((wf) => wf.id === confirmDeleteId);

  return (
    <div className="wf-list-page">
      <div className="wf-list-header">
        <h2 className="wf-list-title">Workflows</h2>
        <button className="auto-new-btn" onClick={() => setShowCreate((v) => !v)}>
          {showCreate ? 'Cancel' : '+ New'}
        </button>
      </div>

      {showCreate && (
        <form className="wf-create-form" onSubmit={handleCreate}>
          <input
            className="wf-config-input"
            placeholder="Workflow name"
            value={newName}
            onChange={(e) => setNewName(e.target.value)}
            autoFocus
          />
          <input
            className="wf-config-input"
            placeholder="Description (optional)"
            value={newDesc}
            onChange={(e) => setNewDesc(e.target.value)}
          />
          <button
            type="submit"
            className="auto-new-btn"
            disabled={createMutation.isPending || !newName.trim()}
          >
            {createMutation.isPending ? 'Creating…' : 'Create'}
          </button>
        </form>
      )}

      {isLoading && <div className="wf-list-empty">Loading…</div>}

      {!isLoading && workflows.length === 0 && (
        <div className="wf-list-empty">No workflows yet. Create one above.</div>
      )}

      {workflows.map((wf) => (
        <div
          key={wf.id}
          className="wf-row"
          onClick={() => void navigate({to: '/workflow/$id', params: {id: wf.id}})}
        >
          <div className="wf-row-info">
            <div className="wf-row-name">{wf.name}</div>
            {wf.description && <div className="wf-row-desc">{wf.description}</div>}
            <div className="wf-row-meta">{formatRelativeTime(wf.updated_at)}</div>
          </div>
          <div className="wf-row-actions">
            <button
              className="wf-delete-node-btn wf-delete-node-btn--inline"
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
        <div className="wf-modal-backdrop" onClick={() => setConfirmDeleteId(null)}>
          <div className="wf-modal" onClick={(e) => e.stopPropagation()}>
            <div className="wf-modal-title">Delete workflow?</div>
            <p style={{fontSize: '0.83rem', color: 'var(--text-dim)', margin: 0}}>
              <strong style={{color: 'var(--text)'}}>{confirmDeleteWorkflow.name}</strong> and all
              its run history will be permanently deleted.
            </p>
            <div className="wf-modal-actions">
              <button className="wf-save-btn" onClick={() => setConfirmDeleteId(null)}>
                Cancel
              </button>
              <button
                className="wf-delete-node-btn"
                style={{marginTop: 0}}
                disabled={deleteMutation.isPending}
                onClick={() => deleteMutation.mutate(confirmDeleteId)}
              >
                {deleteMutation.isPending ? 'Deleting…' : 'Delete'}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
