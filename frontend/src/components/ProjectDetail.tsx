import * as stylex from '@stylexjs/stylex';
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
import {
  mapProjectDetail,
  projectQuery,
  projectQueryVars,
  refreshProject,
} from '../relay/ProjectQuery';
import {refreshProjects} from '../relay/ProjectsQuery';
import {commitSetConversationProject} from '../relay/SetConversationProjectMutation';
import {commitStartTask} from '../relay/StartTaskMutation';
import {commitUpdateProject} from '../relay/UpdateProjectMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {FolderIcon, PlusIcon, TrashIcon, XIcon} from './icons';
import {InputBox} from './InputBox';
import {item} from './memory.styles';
import {detail, projects} from './project.styles';
import {useQueryRetry} from './QueryBoundary';
import {btn, field, iconBtn, page} from './ui';

interface Props {
  id: string;
}

export function ProjectDetail({id}: Props) {
  const navigate = useNavigate();

  const data = useLazyLoadQuery<TProjectQuery>(projectQuery, projectQueryVars(id), {
    fetchPolicy: 'store-and-network',
    fetchKey: useQueryRetry(),
  });
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
      <div {...stylex.props(page.scroll)}>
        <div {...stylex.props(page.empty)}>
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
    <div {...stylex.props(page.scroll, detail.page)}>
      <header {...stylex.props(page.header)}>
        <div {...stylex.props(page.headerMain)}>
          <nav {...stylex.props(detail.breadcrumb)}>
            <Link to="/projects" {...stylex.props(detail.breadcrumbLink)}>
              Projects
            </Link>{' '}
            /
          </nav>
          <h1 {...stylex.props(page.title, detail.title)}>
            <FolderIcon size={18} style={projects.icon} /> {project.name}
          </h1>
          {project.description && <p {...stylex.props(page.subtitle)}>{project.description}</p>}
        </div>
        <div {...stylex.props(page.headerActions)}>
          <button
            {...stylex.props(btn.base)}
            onClick={() => {
              setMetaDraft({name: project.name, description: project.description ?? ''});
              setEditMeta(true);
            }}
          >
            Edit
          </button>
          <button
            {...stylex.props(btn.base)}
            title="Delete project (conversations are kept)"
            onClick={() => setConfirmDelete(true)}
          >
            <TrashIcon size={14} /> Delete
          </button>
        </div>
      </header>

      {actionError && <div {...stylex.props(page.error)}>{actionError}</div>}

      <div {...stylex.props(detail.grid)}>
        <section {...stylex.props(page.section)}>
          <h2 {...stylex.props(page.sectionTitle)}>Instructions</h2>
          <p {...stylex.props(detail.sectionHint)}>
            Injected into every conversation in this project. Yours to edit — the agent reads but
            never changes them.
          </p>
          <textarea
            {...stylex.props(field.textarea, detail.textarea)}
            value={instructions}
            onChange={(e) => setInstructions(e.target.value)}
            rows={8}
            spellCheck={false}
            placeholder="Guidance the agent should follow in every conversation of this project…"
          />
          <div {...stylex.props(detail.sectionActions)}>
            <button
              {...stylex.props(btn.base, btn.primary)}
              disabled={!instructionsDirty || updateAction.pending}
              onClick={() => void updateAction.run({instructions})}
            >
              Save instructions
            </button>
          </div>
        </section>

        <section {...stylex.props(page.section)}>
          <h2 {...stylex.props(page.sectionTitle)}>Project memory</h2>
          <p {...stylex.props(detail.sectionHint)}>
            The agent's shared notepad across this project's conversations — it appends and
            reorganizes this itself via <code>project_memory</code>. You can prune it here.
          </p>
          <textarea
            {...stylex.props(field.textarea, detail.textarea)}
            value={memory}
            onChange={(e) => setMemory(e.target.value)}
            rows={8}
            spellCheck={false}
            placeholder="Nothing saved yet — the agent will write here as it learns."
          />
          <div {...stylex.props(detail.sectionActions)}>
            <button
              {...stylex.props(btn.base, btn.primary)}
              disabled={!memoryDirty || updateAction.pending}
              onClick={() => void updateAction.run({memory})}
            >
              Save memory
            </button>
          </div>
        </section>
      </div>

      <section {...stylex.props(page.section)}>
        <h2 {...stylex.props(page.sectionTitle)}>
          Conversations <span {...stylex.props(page.count)}>{project.conversations.length}</span>
          <button
            {...stylex.props(btn.base, detail.addExistingBtn)}
            onClick={() => setShowAddExisting(true)}
          >
            <PlusIcon size={13} /> Add existing
          </button>
        </h2>
        {project.conversations.length === 0 ? (
          <div {...stylex.props(page.empty)}>
            <p>No conversations yet — start one below, or add an existing one.</p>
          </div>
        ) : (
          <ul {...stylex.props(detail.convList)}>
            {project.conversations.map((c) => (
              <li key={c.id} {...stylex.props(detail.convRow)}>
                <Link to="/c/$id" params={{id: c.id}} {...stylex.props(detail.convLink)}>
                  <span {...stylex.props(detail.convTitle)}>
                    {c.title || 'Untitled conversation'}
                  </span>
                  <span {...stylex.props(item.meta)}>
                    {c.message_count} message{c.message_count === 1 ? '' : 's'} ·{' '}
                    {formatRelativeTime(c.created_at)}
                  </span>
                </Link>
                <button
                  {...stylex.props(iconBtn.base)}
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

      <footer {...stylex.props(detail.footer)}>
        <p {...stylex.props(detail.newChatHint)}>Start a new conversation in this project</p>
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
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Name</span>
          <input
            {...stylex.props(field.input)}
            value={metaDraft.name}
            onChange={(e) => setMetaDraft({...metaDraft, name: e.target.value})}
            autoFocus
            spellCheck={false}
          />
        </div>
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Description</span>
          <input
            {...stylex.props(field.input)}
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
    return (
      <div {...stylex.props(page.empty)}>Every conversation already belongs to a project.</div>
    );
  }
  return (
    <ul {...stylex.props(detail.convList)}>
      {candidates.map((c) => (
        <li key={c.id} {...stylex.props(detail.convRow)}>
          <span {...stylex.props(detail.convTitle)}>{c.title || 'Untitled conversation'}</span>
          <button {...stylex.props(btn.base)} disabled={busy} onClick={() => onAdd(c.id)}>
            <PlusIcon size={13} /> Add
          </button>
        </li>
      ))}
    </ul>
  );
}
