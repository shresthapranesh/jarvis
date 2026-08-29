import * as stylex from '@stylexjs/stylex';
import {marked} from 'marked';
import {useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {AgentMemoryQuery as TAgentMemoryQuery} from '../__generated__/AgentMemoryQuery.graphql';
import type {MemoriesQuery as TMemoriesQuery} from '../__generated__/MemoriesQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import type {MemoryItem, MemoryKind} from '../lib/types';
import {commitAddMemory} from '../relay/AddMemoryMutation';
import {agentMemoryQuery, refreshAgentMemory} from '../relay/AgentMemoryQuery';
import {commitConsolidateMemory} from '../relay/ConsolidateMemoryMutation';
import {commitDeleteAgentMemory} from '../relay/DeleteAgentMemoryMutation';
import {commitDeleteMemory} from '../relay/DeleteMemoryMutation';
import {memoriesQuery, refreshMemories} from '../relay/MemoriesQuery';
import {commitUpdateMemoryItem} from '../relay/UpdateMemoryItemMutation';
import {commitUpdateMemory} from '../relay/UpdateMemoryMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {EditIcon, PlusIcon, TrashIcon} from './icons';
import {item, kindDotStyle, kindDot, memory, seg} from './memory.styles';
import {useQueryRetry} from './QueryBoundary';
import {btn, codeField, field, iconBtn, page, prose} from './ui';
import {type} from '../theme/tokens.stylex';

const KIND_INFO: Record<MemoryKind, {label: string; hint: string}> = {
  fact: {label: 'Fact', hint: 'surfaced by relevance each turn'},
  core: {label: 'Core', hint: 'in every system prompt'},
};

type Editor = {mode: 'add'} | {mode: 'edit'; item: MemoryItem};

export function MemoryView() {
  // Suspends on first load and throws on failure — the /memory route wraps this
  // in a QueryBoundary, which supplies the retry fetchKey.
  const fetchKey = useQueryRetry();
  const memoryData = useLazyLoadQuery<TMemoriesQuery>(
    memoriesQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey},
  );
  const blobData = useLazyLoadQuery<TAgentMemoryQuery>(
    agentMemoryQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey},
  );

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [text, setText] = useState('');
  const [kind, setKind] = useState<MemoryKind>('fact');
  const [deleteTarget, setDeleteTarget] = useState<MemoryItem | null>(null);

  function closeEditor() {
    setEditor(null);
    setActionError(null);
  }

  function openAdd() {
    setText('');
    setKind('fact');
    setActionError(null);
    setEditor({mode: 'add'});
  }

  function openEdit(m: MemoryItem) {
    setText(m.text);
    setActionError(null);
    setEditor({mode: 'edit', item: m});
  }

  // Each write leaves the Relay store correct via its own updater (or, for an
  // edit, via plain normalization), so none of these refetch the list.
  const addAction = useAsyncAction(() => commitAddMemory(text.trim(), kind), {
    onSuccess: closeEditor,
    onError: (e) => setActionError(e.message),
  });

  const updateAction = useAsyncAction((id: string, t: string) => commitUpdateMemoryItem(id, t), {
    onSuccess: closeEditor,
    onError: (e) => setActionError(e.message),
  });

  const deleteAction = useAsyncAction((id: string) => commitDeleteMemory(id), {
    onSuccess: () => {
      setDeleteTarget(null);
      setActionError(null);
    },
    onError: (e) => {
      setDeleteTarget(null);
      setActionError(e.message);
    },
  });

  // The exception: consolidation rewrites the whole set server-side, so there is
  // no delta to apply — re-read both queries into the store.
  const consolidateAction = useAsyncAction(
    async () => {
      await commitConsolidateMemory();
      await Promise.all([refreshMemories(), refreshAgentMemory()]);
    },
    {
      onSuccess: () => setActionError(null),
      onError: (e) => setActionError(e.message),
    },
  );

  const all: MemoryItem[] = memoryData.memories.map((m) => ({
    id: m.id,
    kind: m.kind as MemoryKind,
    text: m.text,
    updated_at: m.updatedAt,
  }));
  const core = all.filter((m) => m.kind === 'core');
  const facts = all.filter((m) => m.kind === 'fact');
  const blob = blobData.agentMemory;
  const showBlobFallback = all.length === 0 && blob.exists && blob.content.trim().length > 0;

  function submitEditor() {
    if (!editor) return;
    if (editor.mode === 'add') void addAction.run();
    else void updateAction.run(editor.item.id, text.trim());
  }

  function renderItem(m: MemoryItem) {
    return (
      <li key={m.id} {...stylex.props(item.root)}>
        <div {...stylex.props(item.main)}>
          <span {...stylex.props(item.text)}>{m.text}</span>
          <span {...stylex.props(item.meta)}>
            Updated {new Date(m.updated_at).toLocaleDateString()}
          </span>
        </div>
        <div {...stylex.props(item.actions)}>
          <button {...stylex.props(iconBtn.base)} title="Edit memory" onClick={() => openEdit(m)}>
            <EditIcon size={14} />
          </button>
          <button
            {...stylex.props(iconBtn.base, iconBtn.danger)}
            title="Delete memory"
            onClick={() => setDeleteTarget(m)}
          >
            <TrashIcon size={14} />
          </button>
        </div>
      </li>
    );
  }

  function renderSection(title: string, kindKey: MemoryKind, list: MemoryItem[]) {
    return (
      <section {...stylex.props(page.section)}>
        <h2 {...stylex.props(page.sectionTitle)}>
          <span {...stylex.props(kindDot.base, kindDotStyle(kindKey))} />
          {title} <span {...stylex.props(page.count)}>{list.length}</span>
          <span {...stylex.props(page.sectionHint)}>{KIND_INFO[kindKey].hint}</span>
        </h2>
        {list.length === 0 ? (
          <p {...stylex.props(memory.sectionEmpty)}>Nothing here yet.</p>
        ) : (
          <ul {...stylex.props(page.list)}>{list.map(renderItem)}</ul>
        )}
      </section>
    );
  }

  const editorOpen = editor !== null;
  const editKind = editor?.mode === 'edit' ? editor.item.kind : kind;

  return (
    <div {...stylex.props(page.scroll)}>
      <header {...stylex.props(page.header)}>
        <div {...stylex.props(page.headerMain)}>
          <h1 {...stylex.props(page.title)}>Memory</h1>
          <p {...stylex.props(page.subtitle)}>
            The agent's long-term memory — discrete items embedded for retrieval.{' '}
            <strong>Core</strong> items are injected into every system prompt; <strong>fact</strong>{' '}
            items are surfaced by relevance each turn. The agent can also write them via{' '}
            <code>remember(…)</code>, or the scheduled consolidation job extracts them from recent
            conversations.
          </p>
        </div>
        <div {...stylex.props(page.headerActions)}>
          <button
            {...stylex.props(btn.base)}
            onClick={() => void consolidateAction.run()}
            disabled={consolidateAction.pending}
            title="Run the LLM that extracts new memory items from recent conversations"
          >
            {consolidateAction.pending ? 'Consolidating…' : 'Consolidate'}
          </button>
          <button {...stylex.props(btn.base, btn.primary)} onClick={openAdd}>
            <PlusIcon size={14} /> Add memory
          </button>
        </div>
      </header>

      {actionError && !editorOpen && <div {...stylex.props(page.error)}>{actionError}</div>}

      {showBlobFallback ? (
        <LegacyBlob blob={blob} primary />
      ) : all.length === 0 ? (
        <div {...stylex.props(page.empty)}>
          <p>No memories yet.</p>
          <p>
            Add one, ask the agent to <code>remember</code> something, or run consolidation after
            some conversation history accumulates.
          </p>
          <button {...stylex.props(btn.base, btn.primary, styles.emptyBtn)} onClick={openAdd}>
            <PlusIcon size={14} /> Add memory
          </button>
        </div>
      ) : (
        <>
          {renderSection('Core', 'core', core)}
          {renderSection('Facts', 'fact', facts)}
        </>
      )}

      {/* The blob is still the whole of memory on a keyless setup, and the only
          copy of anything consolidation hasn't split yet — so it needs the same
          view / replace / clear the `memory` CLI subcommands give it, not just a
          read-only fallback when the discrete list happens to be empty. */}
      {!showBlobFallback && blob.exists && <LegacyBlob blob={blob} />}

      <FormModal
        open={editorOpen}
        title={editor?.mode === 'edit' ? 'Edit memory' : 'Add memory'}
        subtitle="One self-contained fact per item — it's embedded as a whole for retrieval."
        submitLabel={editor?.mode === 'edit' ? 'Save changes' : 'Add memory'}
        submitDisabled={!text.trim()}
        pending={addAction.pending || updateAction.pending}
        error={actionError}
        onSubmit={submitEditor}
        onClose={closeEditor}
      >
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Memory</span>
          <textarea
            {...stylex.props(field.textarea)}
            value={text}
            onChange={(e) => setText(e.target.value)}
            spellCheck={false}
            rows={4}
            autoFocus
            placeholder="e.g. Prefers responses in Spanish when discussing travel."
          />
        </div>
        <div {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Kind</span>
          {editor?.mode === 'edit' ? (
            <span {...stylex.props(memory.kindStatic)}>
              <span {...stylex.props(kindDot.base, kindDotStyle(editKind))} />
              {KIND_INFO[editKind].label} — {KIND_INFO[editKind].hint}
            </span>
          ) : (
            <div {...stylex.props(seg.root)} role="radiogroup" aria-label="Memory kind">
              {(Object.keys(KIND_INFO) as MemoryKind[]).map((k) => (
                <button
                  key={k}
                  type="button"
                  role="radio"
                  aria-checked={kind === k}
                  {...stylex.props(seg.opt, kind === k && seg.optActive)}
                  onClick={() => setKind(k)}
                >
                  <span {...stylex.props(seg.label)}>
                    <span {...stylex.props(kindDot.base, kindDotStyle(k))} />
                    {KIND_INFO[k].label}
                  </span>
                  <span {...stylex.props(seg.hint)}>{KIND_INFO[k].hint}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </FormModal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Delete memory"
        message={
          <p>
            This removes the item from the agent's memory permanently:{' '}
            <strong>
              {deleteTarget && deleteTarget.text.length > 120
                ? `${deleteTarget.text.slice(0, 120)}…`
                : deleteTarget?.text}
            </strong>
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

function LegacyBlob({
  blob,
  primary = false,
}: {
  blob: TAgentMemoryQuery['response']['agentMemory'];
  primary?: boolean;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(blob.content);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);

  async function save() {
    setBusy(true);
    setError(null);
    try {
      await commitUpdateMemory(draft);
      await refreshAgentMemory();
      setEditing(false);
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setBusy(false);
    }
  }

  async function clear() {
    setBusy(true);
    setError(null);
    try {
      await commitDeleteAgentMemory();
      await refreshAgentMemory();
      setDraft('');
      setEditing(false);
    } catch (e) {
      setError((e as Error).message || String(e));
    } finally {
      setBusy(false);
      setConfirmClear(false);
    }
  }

  return (
    <div {...stylex.props(page.section)}>
      <h2 {...stylex.props(page.sectionTitle)}>
        Legacy AGENTS.md
        <span {...stylex.props(page.sectionHint)}>{blob.content.length} chars</span>
      </h2>
      <p {...stylex.props(page.subtitle)}>
        {primary ? (
          <>
            No discrete items yet (no embedding model configured), so this blob <em>is</em> the
            agent&apos;s memory. It will be split into items on the next consolidation once
            embeddings are available.
          </>
        ) : (
          <>
            The free-text blob kept in the LangGraph store — the keyless fallback, and whatever
            predates the split into discrete items. Same entry as{' '}
            <code>main.py memory show/set/reset</code>.
          </>
        )}
      </p>

      {error && <div {...stylex.props(page.error)}>{error}</div>}

      {editing ? (
        <>
          <textarea
            {...stylex.props(codeField.input, codeField.multiline)}
            rows={16}
            value={draft}
            disabled={busy}
            spellCheck={false}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div {...stylex.props(codeField.actions)}>
            <button
              {...stylex.props(btn.base, btn.primary)}
              disabled={busy || !draft.trim()}
              title={
                draft.trim()
                  ? ''
                  : 'Use Clear to remove the entry — an empty blob is not the same as no blob.'
              }
              onClick={() => void save()}
            >
              Save
            </button>
            <button
              {...stylex.props(btn.base)}
              disabled={busy}
              onClick={() => {
                setDraft(blob.content);
                setEditing(false);
                setError(null);
              }}
            >
              Cancel
            </button>
          </div>
        </>
      ) : (
        <>
          <div
            {...stylex.props(prose.vars, styles.blobBody)}
            data-md
            dangerouslySetInnerHTML={{__html: marked.parse(blob.content) as string}}
          />
          <div {...stylex.props(codeField.actions)}>
            <button
              {...stylex.props(btn.base)}
              onClick={() => {
                setDraft(blob.content);
                setEditing(true);
              }}
            >
              <EditIcon size={14} /> Edit
            </button>
            <button
              {...stylex.props(btn.base)}
              disabled={!blob.exists}
              onClick={() => setConfirmClear(true)}
            >
              <TrashIcon size={14} /> Clear
            </button>
          </div>
        </>
      )}

      <ConfirmDialog
        open={confirmClear}
        title="Clear AGENTS.md memory"
        message="Deletes the blob entry outright. The agent falls back to its system prompt alone. Discrete memory items are untouched."
        confirmLabel="Clear"
        danger
        onConfirm={() => void clear()}
        onCancel={() => setConfirmClear(false)}
      />
    </div>
  );
}

const styles = stylex.create({
  /** The empty state's call to action sits a little clear of the prose. */
  emptyBtn: {marginBlockStart: 8},
  blobBody: {fontSize: type.tBody, lineHeight: 1.55},
});
