import {useMutation, useQuery, useQueryClient} from '@tanstack/react-query';
import {useState} from 'react';

import {commitCreateSkill} from '../relay/CreateSkillMutation';
import {commitDeleteSkill} from '../relay/DeleteSkillMutation';
import {fetchSkills} from '../relay/SkillsQuery';
import {commitUpdateSkill} from '../relay/UpdateSkillMutation';
import type {Skill} from '../lib/types';

interface Draft {
  name: string;
  description: string;
  body: string;
  enabled: boolean;
}

const EMPTY_DRAFT: Draft = {name: '', description: '', body: '', enabled: true};

export function SkillsView() {
  const queryClient = useQueryClient();

  const {data: skills, isLoading, error} = useQuery<Skill[]>({
    queryKey: ['skills'],
    queryFn: fetchSkills,
  });

  const [actionError, setActionError] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft>(EMPTY_DRAFT);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editDraft, setEditDraft] = useState<Draft>(EMPTY_DRAFT);

  const invalidate = () => queryClient.invalidateQueries({queryKey: ['skills']});

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
      setDraft(EMPTY_DRAFT);
      setActionError(null);
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const updateMutation = useMutation({
    mutationFn: ({id, patch}: {id: string; patch: Partial<Draft>}) =>
      commitUpdateSkill(id, patch),
    onSuccess: async () => {
      await invalidate();
      setEditingId(null);
      setActionError(null);
    },
    onError: (e: Error) => setActionError(e.message),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => commitDeleteSkill(id),
    onSuccess: async () => {
      await invalidate();
      setActionError(null);
    },
    onError: (e: Error) => setActionError(e.message),
  });

  function startEdit(s: Skill) {
    setEditingId(s.id);
    setEditDraft({
      name: s.name,
      description: s.description,
      body: s.body,
      enabled: s.enabled,
    });
    setActionError(null);
  }

  function saveEdit(id: string) {
    updateMutation.mutate({
      id,
      patch: {
        name: editDraft.name.trim(),
        description: editDraft.description.trim(),
        body: editDraft.body,
        enabled: editDraft.enabled,
      },
    });
  }

  const all = skills ?? [];
  const createValid =
    draft.name.trim() && draft.description.trim() && draft.body.trim();

  function renderItem(s: Skill) {
    if (editingId === s.id) {
      const editValid =
        editDraft.name.trim() && editDraft.description.trim() && editDraft.body.trim();
      return (
        <li key={s.id} className="memory-item" style={{flexDirection: 'column', alignItems: 'stretch', gap: 6}}>
          <input
            className="auto-form-input"
            value={editDraft.name}
            onChange={(e) => setEditDraft({...editDraft, name: e.target.value})}
            placeholder="name (e.g. weekly-market-recap)"
          />
          <input
            className="auto-form-input"
            value={editDraft.description}
            onChange={(e) => setEditDraft({...editDraft, description: e.target.value})}
            placeholder="description — when to use this skill"
          />
          <textarea
            className="memory-item-textarea"
            value={editDraft.body}
            onChange={(e) => setEditDraft({...editDraft, body: e.target.value})}
            spellCheck={false}
            rows={6}
            placeholder="body — the full procedure (markdown)"
          />
          <label style={{display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.8rem'}}>
            <input
              type="checkbox"
              checked={editDraft.enabled}
              onChange={(e) => setEditDraft({...editDraft, enabled: e.target.checked})}
            />
            Enabled
          </label>
          <div className="memory-item-actions">
            <button
              className="artifact-btn primary"
              onClick={() => saveEdit(s.id)}
              disabled={updateMutation.isPending || !editValid}
            >
              Save
            </button>
            <button className="artifact-btn" onClick={() => setEditingId(null)}>
              Cancel
            </button>
          </div>
        </li>
      );
    }
    return (
      <li key={s.id} className="memory-item" style={{flexDirection: 'column', alignItems: 'stretch', gap: 4}}>
        <div style={{display: 'flex', alignItems: 'center', gap: 8}}>
          <strong style={{fontFamily: 'var(--mono, monospace)'}}>{s.name}</strong>
          {!s.enabled && (
            <span style={{fontSize: '0.7rem', color: 'var(--text-dim)', border: '1px solid var(--border)', borderRadius: 4, padding: '0 5px'}}>
              disabled
            </span>
          )}
          <div className="memory-item-actions" style={{marginLeft: 'auto'}}>
            <button
              className="artifact-btn"
              onClick={() => updateMutation.mutate({id: s.id, patch: {enabled: !s.enabled}})}
              disabled={updateMutation.isPending}
            >
              {s.enabled ? 'Disable' : 'Enable'}
            </button>
            <button className="artifact-btn" onClick={() => startEdit(s)}>
              Edit
            </button>
            <button
              className="artifact-btn"
              onClick={() => {
                if (window.confirm(`Delete skill "${s.name}"?`)) deleteMutation.mutate(s.id);
              }}
            >
              Delete
            </button>
          </div>
        </div>
        <span className="memory-item-text" style={{color: 'var(--text-dim)', fontSize: '0.82rem'}}>
          {s.description}
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
      </header>

      {actionError && <div className="memory-error">{actionError}</div>}

      <div className="memory-add" style={{flexDirection: 'column', alignItems: 'stretch', gap: 6}}>
        <input
          className="auto-form-input"
          value={draft.name}
          onChange={(e) => setDraft({...draft, name: e.target.value})}
          placeholder="name (e.g. weekly-market-recap)"
        />
        <input
          className="auto-form-input"
          value={draft.description}
          onChange={(e) => setDraft({...draft, description: e.target.value})}
          placeholder="description — when should the agent reach for this skill?"
        />
        <textarea
          className="memory-item-textarea"
          value={draft.body}
          onChange={(e) => setDraft({...draft, body: e.target.value})}
          spellCheck={false}
          rows={5}
          placeholder="body — the full procedure / instructions (markdown)"
        />
        <div className="memory-add-actions">
          <label style={{display: 'flex', alignItems: 'center', gap: 6, fontSize: '0.8rem'}}>
            <input
              type="checkbox"
              checked={draft.enabled}
              onChange={(e) => setDraft({...draft, enabled: e.target.checked})}
            />
            Enabled
          </label>
          <button
            className="artifact-btn primary"
            onClick={() => createMutation.mutate()}
            disabled={createMutation.isPending || !createValid}
          >
            {createMutation.isPending ? 'Adding…' : 'Add skill'}
          </button>
        </div>
      </div>

      {isLoading ? (
        <div className="memory-empty">Loading…</div>
      ) : error ? (
        <div className="memory-empty">Failed to load skills: {(error as Error).message}</div>
      ) : all.length === 0 ? (
        <div className="memory-empty">
          <p>No skills yet.</p>
          <p>Add one above, or ask the agent to <code>create_skill</code> something reusable.</p>
        </div>
      ) : (
        <div className="memory-section">
          <h2 className="memory-section-title">
            All <span className="memory-count">{all.length}</span>
          </h2>
          <ul className="memory-list">{all.map(renderItem)}</ul>
        </div>
      )}
    </div>
  );
}
