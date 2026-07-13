import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useNavigate} from '@tanstack/react-router';
import {useState} from 'react';

import {formatRelativeTime} from '../lib/api';
import type {Project} from '../lib/types';
import {commitCreateProject} from '../relay/CreateProjectMutation';
import {commitDeleteProject} from '../relay/DeleteProjectMutation';
import {fetchProjects} from '../relay/ProjectsQuery';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {FolderIcon, PlusIcon, TrashIcon} from './icons';

interface Draft {
  name: string;
  description: string;
  instructions: string;
}

const EMPTY_DRAFT: Draft = {name: '', description: '', instructions: ''};

export function ProjectsView() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();

  const {
    data: projects,
    isLoading,
    error,
  } = useQuery<Project[]>({
    queryKey: ['projects'],
    queryFn: fetchProjects,
  });

  const [actionError, setActionError] = useState<string | null>(null);
  const [showCreate, setShowCreate] = useState(false);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [deleteTarget, setDeleteTarget] = useState<Project | null>(null);

  const invalidate = () => queryClient.invalidateQueries({queryKey: ['projects']});

  const createMutation = useMutation({
    mutationFn: () =>
      commitCreateProject({
        name: draft.name.trim(),
        description: draft.description.trim() || null,
        instructions: draft.instructions,
      }),
    onSuccess: async (created) => {
      await invalidate();
      setShowCreate(false);
      void navigate({to: '/projects/$id', params: {id: created.id}});
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => commitDeleteProject(id),
    onSuccess: async () => {
      await invalidate();
      setDeleteTarget(null);
      setActionError(null);
    },
    onError: (e: Error) => {
      setDeleteTarget(null);
      setActionError(e.message);
    },
  });

  function openCreate() {
    setDraft(EMPTY_DRAFT);
    setActionError(null);
    setShowCreate(true);
  }

  const all = projects ?? [];

  return (
    <div className="page memory-page">
      <header className="memory-header">
        <div>
          <h1>Projects</h1>
          <p className="memory-subtitle">
            Group related conversations under shared <strong>instructions</strong> and a shared{' '}
            <strong>project memory</strong> the agent maintains itself — everything it learns in one
            conversation is available to all the others.
          </p>
        </div>
        <div className="memory-header-actions">
          <button className="artifact-btn primary" onClick={openCreate}>
            <PlusIcon size={14} /> New project
          </button>
        </div>
      </header>

      {actionError && <div className="memory-error">{actionError}</div>}

      {isLoading ? (
        <div className="memory-empty">Loading…</div>
      ) : error ? (
        <div className="memory-empty">Failed to load projects: {(error as Error).message}</div>
      ) : all.length === 0 ? (
        <div className="memory-empty">
          <p>No projects yet.</p>
          <p>Create one to give a set of conversations shared context.</p>
          <button className="artifact-btn primary" onClick={openCreate}>
            <PlusIcon size={14} /> New project
          </button>
        </div>
      ) : (
        <ul className="project-grid">
          {all.map((p) => (
            <li
              key={p.id}
              className="skill-card project-card"
              onClick={() => void navigate({to: '/projects/$id', params: {id: p.id}})}
            >
              <div className="skill-card-head">
                <span className="skill-card-name project-card-name">
                  <FolderIcon size={14} /> {p.name}
                </span>
                <div className="skill-card-controls">
                  <button
                    className="icon-btn icon-btn--danger"
                    title="Delete project"
                    onClick={(e) => {
                      e.stopPropagation();
                      setDeleteTarget(p);
                    }}
                  >
                    <TrashIcon size={14} />
                  </button>
                </div>
              </div>
              {p.description && <p className="skill-card-desc">{p.description}</p>}
              <span className="memory-item-meta">
                {p.conversation_count} conversation{p.conversation_count === 1 ? '' : 's'} · updated{' '}
                {formatRelativeTime(p.updated_at)}
              </span>
            </li>
          ))}
        </ul>
      )}

      <FormModal
        open={showCreate}
        title="New project"
        subtitle="Instructions apply to every conversation in the project; you can refine them any time."
        submitLabel="Create project"
        submitDisabled={!draft.name.trim()}
        pending={createMutation.isPending}
        error={actionError}
        onSubmit={() => createMutation.mutate()}
        onClose={() => {
          setShowCreate(false);
          setActionError(null);
        }}
      >
        <div className="auto-form-group">
          <span className="auto-form-label">Name</span>
          <input
            className="auto-form-input"
            value={draft.name}
            onChange={(e) => setDraft({...draft, name: e.target.value})}
            autoFocus
            spellCheck={false}
            placeholder="Q3 launch research"
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Description (optional)</span>
          <input
            className="auto-form-input"
            value={draft.description}
            onChange={(e) => setDraft({...draft, description: e.target.value})}
            placeholder="What this project is about"
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Instructions (optional)</span>
          <textarea
            className="auto-form-textarea"
            value={draft.instructions}
            onChange={(e) => setDraft({...draft, instructions: e.target.value})}
            rows={5}
            placeholder="Guidance the agent should follow in every conversation of this project…"
          />
        </div>
      </FormModal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete project"
        message={
          <p>
            Delete <strong>{deleteTarget?.name}</strong>? Its conversations are kept — they are just
            removed from the project.
          </p>
        }
        confirmLabel="Delete"
        danger
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
