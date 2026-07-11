import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useState} from 'react';

import {commitCreateSkill} from '../relay/CreateSkillMutation';
import {commitDeleteSkill} from '../relay/DeleteSkillMutation';
import {fetchSkills} from '../relay/SkillsQuery';
import {commitUpdateSkill} from '../relay/UpdateSkillMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
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
  const queryClient = useQueryClient();

  const {data: skills, isLoading, error} = useQuery<Skill[]>({
    queryKey: ['skills'],
    queryFn: fetchSkills,
  });

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [deleteTarget, setDeleteTarget] = useState<Skill | null>(null);

  const invalidate = () => queryClient.invalidateQueries({queryKey: ['skills']});

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

  const createMutation = useMutation({
    mutationFn: () =>
      commitCreateSkill({
        name: draft.name.trim(),
        description: draft.description.trim(),
        body: draft.body,
        enabled: draft.enabled,
      }),
    onSuccess: async () => {
      await invalidate();
      closeEditor();
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const updateMutation = useMutation({
    mutationFn: ({id, patch}: {id: string; patch: Partial<Draft>}) =>
      commitUpdateSkill(id, patch),
    onSuccess: async () => {
      await invalidate();
      closeEditor();
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => commitDeleteSkill(id),
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

  function submitEditor() {
    if (!editor) return;
    if (editor.mode === 'add') {
      createMutation.mutate();
    } else {
      updateMutation.mutate({
        id: editor.skill.id,
        patch: {
          name: draft.name.trim(),
          description: draft.description.trim(),
          body: draft.body,
          enabled: draft.enabled,
        },
      });
    }
  }

  const all = skills ?? [];
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
                disabled={updateMutation.isPending}
                onChange={(e) =>
                  updateMutation.mutate({id: s.id, patch: {enabled: e.target.checked}})
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

      {isLoading ? (
        <div className="memory-empty">Loading…</div>
      ) : error ? (
        <div className="memory-empty">Failed to load skills: {(error as Error).message}</div>
      ) : all.length === 0 ? (
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
        pending={createMutation.isPending || updateMutation.isPending}
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
        onConfirm={() => deleteTarget && deleteMutation.mutate(deleteTarget.id)}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
