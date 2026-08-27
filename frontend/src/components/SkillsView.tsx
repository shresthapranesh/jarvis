import * as stylex from '@stylexjs/stylex';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {SkillsQuery as TSkillsQuery} from '../__generated__/SkillsQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import type {Skill} from '../lib/types';
import {commitCreateSkill} from '../relay/CreateSkillMutation';
import {commitDeleteSkill} from '../relay/DeleteSkillMutation';
import {mapSkill, refreshSkills, skillsQuery} from '../relay/SkillsQuery';
import {commitUpdateSkill} from '../relay/UpdateSkillMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {EditIcon, PlusIcon, TrashIcon} from './icons';
import {item, skill as skillStyles} from './memory.styles';
import {useQueryRetry} from './QueryBoundary';
import {btn, field, iconBtn, page, Switch} from './ui';

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

  const draftValid = Boolean(draft.name.trim() && draft.description.trim() && draft.body.trim());
  const editorOpen = editor !== null;

  function renderSkill(s: Skill) {
    return (
      <li {...stylex.props(skillStyles.card, !s.enabled && skillStyles.cardDisabled)} key={s.id}>
        <div {...stylex.props(skillStyles.head)}>
          <span {...stylex.props(skillStyles.name)}>{s.name}</span>
          <div {...stylex.props(skillStyles.controls)}>
            <Switch
              checked={s.enabled}
              disabled={updateAction.pending}
              onChange={(next) => void updateAction.run(s.id, {enabled: next})}
              title={s.enabled ? 'Enabled — click to disable' : 'Disabled — click to enable'}
            />
            <button {...stylex.props(iconBtn.base)} title="Edit skill" onClick={() => openEdit(s)}>
              <EditIcon size={14} />
            </button>
            <button
              {...stylex.props(iconBtn.base, iconBtn.danger)}
              title="Delete skill"
              onClick={() => setDeleteTarget(s)}
            >
              <TrashIcon size={14} />
            </button>
          </div>
        </div>
        <p {...stylex.props(skillStyles.desc)}>{s.description}</p>
        <details {...stylex.props(skillStyles.body)}>
          <summary {...stylex.props(skillStyles.summary)}>Procedure</summary>
          <pre {...stylex.props(skillStyles.pre)}>{s.body}</pre>
        </details>
        <span {...stylex.props(item.meta)}>
          Updated {new Date(s.updated_at).toLocaleDateString()}
        </span>
      </li>
    );
  }

  return (
    <div {...stylex.props(page.scroll)}>
      <header {...stylex.props(page.header)}>
        <div {...stylex.props(page.headerMain)}>
          <h1 {...stylex.props(page.title)}>Skills</h1>
          <p {...stylex.props(page.subtitle)}>
            Reusable, named capabilities the agent can invoke. The <strong>description</strong> is
            the routing key — matched against what you ask to decide when a skill is relevant —
            while the <strong>body</strong> holds the full procedure, loaded only when the skill is
            actually used. The agent can also author its own via <code>create_skill(…)</code>.
          </p>
        </div>
        <div {...stylex.props(page.headerActions)}>
          <button {...stylex.props(btn.base, btn.primary)} onClick={openAdd}>
            <PlusIcon size={14} /> New skill
          </button>
        </div>
      </header>

      {actionError && !editorOpen && <div {...stylex.props(page.error)}>{actionError}</div>}

      {all.length === 0 ? (
        <div {...stylex.props(page.empty)}>
          <p>No skills yet.</p>
          <p>
            Create one, or ask the agent to <code>create_skill</code> something reusable.
          </p>
          <button {...stylex.props(btn.base, btn.primary)} onClick={openAdd}>
            <PlusIcon size={14} /> New skill
          </button>
        </div>
      ) : (
        <div {...stylex.props(page.section)}>
          <h2 {...stylex.props(page.sectionTitle)}>
            All <span {...stylex.props(page.count)}>{all.length}</span>
          </h2>
          <ul {...stylex.props(page.list)}>{all.map(renderSkill)}</ul>
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
          <Switch
            checked={draft.enabled}
            onChange={(next) => setDraft({...draft, enabled: next})}
            label="Enabled"
          />
        }
        onSubmit={submitEditor}
        onClose={closeEditor}
      >
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Name</span>
          <input
            {...stylex.props(field.input, skillStyles.monoField)}
            value={draft.name}
            onChange={(e) => setDraft({...draft, name: e.target.value})}
            autoFocus={editor?.mode === 'add'}
            spellCheck={false}
            placeholder="weekly-market-recap"
          />
        </div>
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Description — when to use it</span>
          <input
            {...stylex.props(field.input)}
            value={draft.description}
            onChange={(e) => setDraft({...draft, description: e.target.value})}
            placeholder="When asked for a recap of this week's market moves…"
          />
        </div>
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Body — the full procedure (markdown)</span>
          <textarea
            {...stylex.props(field.textarea, skillStyles.monoField)}
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
            Delete <strong>{deleteTarget?.name}</strong>? The agent will no longer be able to use
            it.
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
