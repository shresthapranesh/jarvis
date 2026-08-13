import {Link, useNavigate} from '@tanstack/react-router';
import {useEffect, useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ConversationListQuery as TConversationListQuery} from '../__generated__/ConversationListQuery.graphql';
import type {ProjectQuery as TProjectQuery} from '../__generated__/ProjectQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {formatRelativeTime} from '../lib/api';
import type {MediaAttachment} from '../lib/types';
import {uploadStagedAttachment} from '../lib/uploads';
import {conversationListQuery} from '../relay/ConversationListQuery';
import {commitDeleteProject} from '../relay/DeleteProjectMutation';
import {decodeGlobalId} from '../relay/globalId';
import {mapProjectDetail, projectQuery, projectQueryVars, refreshProject} from '../relay/ProjectQuery';
import {refreshProjects} from '../relay/ProjectsQuery';
import {commitSetConversationProject} from '../relay/SetConversationProjectMutation';
import {commitStartTask} from '../relay/StartTaskMutation';
import {commitUpdateProject} from '../relay/UpdateProjectMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {useQueryRetry} from './QueryBoundary';
import {FolderIcon, PlusIcon, TrashIcon, XIcon} from './icons';
import {InputBox} from './InputBox';

interface Props {
  id: string;
}

export function ProjectDetail({id}: Props) {
  const navigate = useNavigate();

  const data = useLazyLoadQuery<TProjectQuery>(
    projectQuery,
    projectQueryVars(id),
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const project = useMemo(
    () => (data.project ? mapProjectDetail(data.project) : null),
    [data.project],
  );

  const [actionError, setActionError] = useState<string | null>(null);
  const [instructions, setInstructions] = useState('');
  const [memory, setMemory] = useState('');
  const [editMeta, setEditMeta] = useState(false);
  const [metaDraft, setMetaDraft] = useState({name: '', description: ''});
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [showAddExisting, setShowAddExisting] = useState(false);
  const [chatBusy, setChatBusy] = useState(false);

  // Seed the editable textareas whenever a fresh copy of the project loads.
  useEffect(() => {
    if (project) {
      setInstructions(project.instructions);
      setMemory(project.memory);
    }
  }, [project]);

  // The project's own `conversations` list and the /projects card counts are
  // server-computed, so membership and metadata edits re-read both.
  const invalidate = () => Promise.all([refreshProject(id), refreshProjects()]);

  const updateAction = useAsyncAction(
    async (patch: {
      name?: string;
      description?: string | null;
      instructions?: string;
      memory?: string;
    }) => {
      await commitUpdateProject(id, patch);
      await invalidate();
    },
    {
      onSuccess: () => {
        setEditMeta(false);
        setActionError(null);
      },
      onError: (e) => setActionError(e.message),
    },
  );

  const deleteAction = useAsyncAction(() => commitDeleteProject(id), {
    onSuccess: () => void navigate({to: '/projects'}),
    onError: (e) => {
      setConfirmDelete(false);
      setActionError(e.message);
    },
  });

  const membershipAction = useAsyncAction(
    async (convId: string, add: boolean) => {
      await commitSetConversationProject(convId, add ? id : null);
      await invalidate();
    },
    {onSuccess: () => setActionError(null), onError: (e) => setActionError(e.message)},
  );

  async function handleNewChat(query: string, model: string, attachments: MediaAttachment[]) {
    setChatBusy(true);
    setActionError(null);
    try {
      const uploads = attachments.length
        ? await Promise.all(
            attachments.map(async (a) => ({uploadId: (await uploadStagedAttachment(a)).uploadId})),
          )
        : null;
      const {taskId, conversationId} = await commitStartTask({
        input: {query, model, attachmentUploads: uploads, projectId: id},
      });
      await navigate({
        to: '/c/$id',
        params: {id: conversationId},
        search: {task: taskId},
      });
    } catch (err) {
      setActionError((err as Error).message);
      setChatBusy(false);
    }
  }

  if (!project) {
    return (
      <div className="page memory-page">
        <div className="memory-empty">
          Project not found.
          <p>
            <Link to="/projects">Back to projects</Link>
          </p>
        </div>
      </div>
    );
  }

  const instructionsDirty = instructions !== project.instructions;
  const memoryDirty = memory !== project.memory;

  return (
    <div className="page memory-page project-detail">
      <header className="memory-header">
        <div>
          <nav className="project-breadcrumb">
            <Link to="/projects">Projects</Link> /
          </nav>
          <h1 className="project-detail-title">
            <FolderIcon size={18} /> {project.name}
          </h1>
          {project.description && <p className="memory-subtitle">{project.description}</p>}
        </div>
        <div className="memory-header-actions">
          <button
            className="artifact-btn"
            onClick={() => {
              setMetaDraft({name: project.name, description: project.description ?? ''});
              setEditMeta(true);
            }}
          >
            Edit
          </button>
          <button
            className="artifact-btn"
            title="Delete project (conversations are kept)"
            onClick={() => setConfirmDelete(true)}
          >
            <TrashIcon size={14} /> Delete
          </button>
        </div>
      </header>

      {actionError && <div className="memory-error">{actionError}</div>}

      <div className="project-detail-grid">
        <section className="memory-section">
          <h2 className="memory-section-title">Instructions</h2>
          <p className="project-section-hint">
            Injected into every conversation in this project. Yours to edit — the agent reads but
            never changes them.
          </p>
          <textarea
            className="auto-form-textarea project-textarea"
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={8}
            spellCheck={false}
            placeholder="Guidance the agent should follow in every conversation of this project…"
          />
          <div className="project-section-actions">
            <button
              className="artifact-btn primary"
              disabled={!instructionsDirty || updateAction.pending}
              onClick={() => void updateAction.run({instructions})}
            >
              Save instructions
            </button>
          </div>
        </section>

        <section className="memory-section">
          <h2 className="memory-section-title">Project memory</h2>
          <p className="project-section-hint">
            The agent's shared notepad across this project's conversations — it appends and
            reorganizes this itself via <code>project_memory</code>. You can prune it here.
          </p>
          <textarea
            className="auto-form-textarea project-textarea"
            value={memory}
            onChange={(e) => setMemory(e.target.value)}
            rows={8}
            spellCheck={false}
            placeholder="Nothing saved yet — the agent will write here as it learns."
          />
          <div className="project-section-actions">
            <button
              className="artifact-btn primary"
              disabled={!memoryDirty || updateAction.pending}
              onClick={() => void updateAction.run({memory})}
            >
              Save memory
            </button>
          </div>
        </section>
      </div>

      <section className="memory-section">
        <h2 className="memory-section-title">
          Conversations <span className="memory-count">{project.conversations.length}</span>
          <button
            className="artifact-btn project-add-existing-btn"
            onClick={() => setShowAddExisting(true)}
          >
            <PlusIcon size={13} /> Add existing
          </button>
        </h2>
        {project.conversations.length === 0 ? (
          <div className="memory-empty">
            <p>No conversations yet — start one below, or add an existing one.</p>
          </div>
        ) : (
          <ul className="project-conv-list">
            {project.conversations.map((c) => (
              <li key={c.id} className="project-conv-row">
                <Link to="/c/$id" params={{id: c.id}} className="project-conv-link">
                  <span className="project-conv-title">{c.title || 'Untitled conversation'}</span>
                  <span className="memory-item-meta">
                    {c.message_count} message{c.message_count === 1 ? '' : 's'} ·{' '}
                    {formatRelativeTime(c.created_at)}
                  </span>
                </Link>
                <button
                  className="icon-btn"
                  title="Remove from project (keeps the conversation)"
                  disabled={membershipAction.pending}
                  onClick={() => void membershipAction.run(c.id, false)}
                >
                  <XIcon size={14} />
                </button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <footer className="page-footer project-detail-footer">
        <p className="project-newchat-hint">Start a new conversation in this project</p>
        <InputBox onSubmit={handleNewChat} disabled={chatBusy} />
      </footer>

      <FormModal
        open={editMeta}
        title="Edit project"
        submitLabel="Save changes"
        submitDisabled={!metaDraft.name.trim()}
        pending={updateAction.pending}
        error={actionError}
        onSubmit={() =>
          void updateAction.run({
            name: metaDraft.name.trim(),
            description: metaDraft.description.trim() || null,
          })
        }
        onClose={() => {
          setEditMeta(false);
          setActionError(null);
        }}
      >
        <div className="auto-form-group">
          <span className="auto-form-label">Name</span>
          <input
            className="auto-form-input"
            value={metaDraft.name}
            onChange={(e) => setMetaDraft({...metaDraft, name: e.target.value})}
            autoFocus
            spellCheck={false}
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Description</span>
          <input
            className="auto-form-input"
            value={metaDraft.description}
            onChange={(e) => setMetaDraft({...metaDraft, description: e.target.value})}
          />
        </div>
      </FormModal>

      <FormModal
        open={showAddExisting}
        title="Add existing conversation"
        subtitle="Only conversations not already in a project are listed."
        submitLabel="Done"
        onSubmit={() => setShowAddExisting(false)}
        onClose={() => setShowAddExisting(false)}
      >
        {/* FormModal renders null while closed, so this only queries once the
            picker is actually opened — the Relay analogue of `enabled:`. */}
        <CandidatePicker
          busy={membershipAction.pending}
          onAdd={(convId) => void membershipAction.run(convId, true)}
        />
      </FormModal>

      <ConfirmDialog
        open={confirmDelete}
        title="Delete project"
        message={
          <p>
            Delete <strong>{project.name}</strong>? Its conversations are kept — they are just
            removed from the project.
          </p>
        }
        confirmLabel="Delete"
        danger
        onConfirm={() => void deleteAction.run()}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}

/**
 * Candidate list for "add existing" — conversations not already in a project.
 *
 * Mounted only while the picker modal is open, which is how a conditional fetch
 * is expressed in Relay. It reads the same ConversationListQuery the sidebar
 * uses, so it renders from the warm store; and because setConversationProject
 * returns the conversation's new `projectId`, Relay normalizes the change onto
 * that record and the filter below drops the row with no refetch.
 */
function CandidatePicker({busy, onAdd}: {busy: boolean; onAdd: (convId: string) => void}) {
  const data = useLazyLoadQuery<TConversationListQuery>(
    conversationListQuery,
    {},
    {fetchPolicy: 'store-and-network'},
  );
  const candidates = useMemo(
    () =>
      data.conversations
        .filter((c) => c.projectId == null)
        .map((c) => ({id: decodeGlobalId(c.id), title: c.title ?? null})),
    [data.conversations],
  );

  if (candidates.length === 0) {
    return <div className="memory-empty">Every conversation already belongs to a project.</div>;
  }
  return (
    <ul className="project-candidate-list">
      {candidates.map((c) => (
        <li key={c.id} className="project-conv-row">
          <span className="project-conv-title">{c.title || 'Untitled conversation'}</span>
          <button className="artifact-btn" disabled={busy} onClick={() => onAdd(c.id)}>
            <PlusIcon size={13} /> Add
          </button>
        </li>
      ))}
    </ul>
  );
}
