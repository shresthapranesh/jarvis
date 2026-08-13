import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {SkillsQuery as TSkillsQuery} from '../__generated__/SkillsQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {commitCreateSkill} from '../relay/CreateSkillMutation';
import {commitDeleteSkill} from '../relay/DeleteSkillMutation';
import {mapSkill, refreshSkills, skillsQuery} from '../relay/SkillsQuery';
import {commitUpdateSkill} from '../relay/UpdateSkillMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {useQueryRetry} from './QueryBoundary';
import {EditIcon, PlusIcon, TrashIcon} from './icons';
import type {Skill} from '../lib/types';

interface Draft {
  name: string;
  description: string;
  body: string;
  enabled: boolean;
}

const EMPTY_DRAFT: Draft = {name: '', description: '', body: '', enabled: true};

type Editor = {mode: 'add'} | {mode: 'edit'; skill: Skill};

export function SkillsView() {
  const data = useLazyLoadQuery<TSkillsQuery>(
    skillsQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const all = useMemo(() => data.skills.map(mapSkill), [data.skills]);

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [deleteTarget, setDeleteTarget] = useState<Skill | null>(null);


  function closeEditor() {
    setEditor(null);
    setActionError(null);
  }

  function openAdd() {
    setDraft(EMPTY_DRAFT);
    setActionError(null);
    setEditor({mode: 'add'});
  }

  function openEdit(s: Skill) {
    setDraft({name: s.name, description: s.description, body: s.body, enabled: s.enabled});
    setActionError(null);
    setEditor({mode: 'edit', skill: s});
  }

  // createSkill returns the new node but `skills` is a plain root list, so the
  // new row still has to be linked in — one refresh is simpler than an updater
  // per mutation, and this list is small and rarely written.
  const createAction = useAsyncAction(
    async () => {
      await commitCreateSkill({
        name: draft.name.trim(),
        description: draft.description.trim(),
        body: draft.body,
        enabled: draft.enabled,
      });
      await refreshSkills();
    },
    {onSuccess: closeEditor, onError: (e) => setActionError(e.message)},
  );

  // Edits need no refresh: the payload carries the same id, so Relay
  // normalizes it onto the existing record.
  const updateAction = useAsyncAction(
    (id: string, patch: Partial<Draft>) => commitUpdateSkill(id, patch),
    {onSuccess: closeEditor, onError: (e) => setActionError(e.message)},
  );

  const deleteAction = useAsyncAction(
    async (id: string) => {
      await commitDeleteSkill(id);
      await refreshSkills();
    },
    {
      onSuccess: () => {
        setDeleteTarget(null);
        setActionError(null);
      },
      onError: (e) => {
        setDeleteTarget(null);
        setActionError(e.message);
      },
    },
  );

  function submitEditor() {
    if (!editor) return;
    if (editor.mode === 'add') {
      void createAction.run();
    } else {
      void updateAction.run(editor.skill.id, {
        name: draft.name.trim(),
        description: draft.description.trim(),
        body: draft.body,
        enabled: draft.enabled,
      });
    }
  }

  const draftValid = Boolean(
    draft.name.trim() && draft.description.trim() && draft.body.trim(),
  );
  const editorOpen = editor !== null;

  function renderSkill(s: Skill) {
    return (
      <li key={s.id} className={`skill-card${s.enabled ? '' : ' skill-card--disabled'}`}>
        <div className="skill-card-head">
          <span className="skill-card-name">{s.name}</span>
          <div className="skill-card-controls">
            <label className="switch" title={s.enabled ? 'Enabled — click to disable' : 'Disabled — click to enable'}>
              <input
                type="checkbox"
                checked={s.enabled}
                disabled={updateAction.pending}
                onChange={(e) =>
                  void updateAction.run(s.id, {enabled: e.target.checked})
                }
              />
              <span className="switch-track" aria-hidden="true" />
            </label>
            <button className="icon-btn" title="Edit skill" onClick={() => openEdit(s)}>
              <EditIcon size={14} />
            </button>
            <button
              className="icon-btn icon-btn--danger"
              title="Delete skill"
              onClick={() => setDeleteTarget(s)}
            >
              <TrashIcon size={14} />
            </button>
          </div>
        </div>
        <p className="skill-card-desc">{s.description}</p>
        <details className="skill-card-body">
          <summary>Procedure</summary>
          <pre>{s.body}</pre>
        </details>
        <span className="memory-item-meta">
          Updated {new Date(s.updated_at).toLocaleDateString()}
        </span>
      </li>
    );
  }

  return (
    <div className="page memory-page">
      <header className="memory-header">
        <div>
          <h1>Skills</h1>
          <p className="memory-subtitle">
            Reusable, named capabilities the agent can invoke. The{' '}
            <strong>description</strong> is the routing key — matched against what you ask
            to decide when a skill is relevant — while the <strong>body</strong> holds the
            full procedure, loaded only when the skill is actually used. The agent can also
            author its own via <code>create_skill(…)</code>.
          </p>
        </div>
        <div className="memory-header-actions">
          <button className="artifact-btn primary" onClick={openAdd}>
            <PlusIcon size={14} /> New skill
          </button>
        </div>
      </header>

      {actionError && !editorOpen && <div className="memory-error">{actionError}</div>}

      {all.length === 0 ? (
        <div className="memory-empty">
          <p>No skills yet.</p>
          <p>
            Create one, or ask the agent to <code>create_skill</code> something reusable.
          </p>
          <button className="artifact-btn primary" onClick={openAdd}>
            <PlusIcon size={14} /> New skill
          </button>
        </div>
      ) : (
        <div className="memory-section">
          <h2 className="memory-section-title">
            All <span className="memory-count">{all.length}</span>
          </h2>
          <ul className="memory-list">{all.map(renderSkill)}</ul>
        </div>
      )}

      <FormModal
        open={editorOpen}
        title={editor?.mode === 'edit' ? `Edit skill` : 'New skill'}
        subtitle="The description decides when the agent reaches for this skill; the body is what it follows."
        wide
        submitLabel={editor?.mode === 'edit' ? 'Save changes' : 'Create skill'}
        submitDisabled={!draftValid}
        pending={createAction.pending || updateAction.pending}
        error={actionError}
        footerExtra={
          <label className="switch switch--labeled">
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(e) => setDraft({...draft, enabled: e.target.checked})}
            />
            <span className="switch-track" aria-hidden="true" />
            Enabled
          </label>
        }
        onSubmit={submitEditor}
        onClose={closeEditor}
      >
        <div className="auto-form-group">
          <span className="auto-form-label">Name</span>
          <input
            className="auto-form-input skill-name-input"
            value={draft.name}
            onChange={(e) => setDraft({...draft, name: e.target.value})}
            autoFocus={editor?.mode === 'add'}
            spellCheck={false}
            placeholder="weekly-market-recap"
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Description — when to use it</span>
          <input
            className="auto-form-input"
            value={draft.description}
            onChange={(e) => setDraft({...draft, description: e.target.value})}
            placeholder="When asked for a recap of this week's market moves…"
          />
        </div>
        <div className="auto-form-group">
          <span className="auto-form-label">Body — the full procedure (markdown)</span>
          <textarea
            className="auto-form-textarea skill-body-textarea"
            value={draft.body}
            onChange={(e) => setDraft({...draft, body: e.target.value})}
            spellCheck={false}
            rows={10}
            placeholder={'1. Pull the week’s index moves…\n2. Summarize the biggest movers…'}
          />
        </div>
      </FormModal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete skill"
        message={
          <p>
            Delete <strong>{deleteTarget?.name}</strong>? The agent will no longer be able
            to use it.
          </p>
        }
        confirmLabel="Delete"
        danger
        onConfirm={() => deleteTarget && void deleteAction.run(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
