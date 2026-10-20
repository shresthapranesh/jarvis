import * as stylex from '@stylexjs/stylex';
import {Link} from '@tanstack/react-router';
import {useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {MemoriesQuery as TMemoriesQuery} from '../__generated__/MemoriesQuery.graphql';
import {useAsyncAction} from '../hooks/useAsyncAction';
import type {MemoryItem, MemoryKind} from '../lib/types';
import {commitAddMemory} from '../relay/AddMemoryMutation';
import {commitConsolidateMemory} from '../relay/ConsolidateMemoryMutation';
import {commitDeleteMemory} from '../relay/DeleteMemoryMutation';
import {memoriesQuery, refreshMemories} from '../relay/MemoriesQuery';
import {commitSortMemoryTopics} from '../relay/SortMemoryTopicsMutation';
import {commitUpdateMemoryItem} from '../relay/UpdateMemoryItemMutation';
import {ConfirmDialog} from './ConfirmDialog';
import {FormModal} from './FormModal';
import {EditIcon, PlusIcon, TrashIcon} from './icons';
import {item, kindDotStyle, kindDot, memory, seg, topics} from './memory.styles';
import {detail} from './project.styles';
import {useQueryRetry} from './QueryBoundary';
import {btn, field, iconBtn, page, PageHeader, TimeAgo} from './ui';

const KIND_INFO: Record<MemoryKind, {label: string; hint: string}> = {
  fact: {label: 'Fact', hint: 'surfaced by relevance each turn'},
  core: {label: 'Core', hint: 'in every system prompt'},
};

/** The `?topic=` of memories no topic has been given yet. */
const UNSORTED = 'unsorted';

/** What each of the server's topics holds, for its card and its page. */
const TOPIC_HINTS: Record<string, string> = {
  profile: 'Who you are and where you live',
  work: 'Your job, team and tools',
  projects: 'What you are building',
  preferences: 'How you want things done',
  people: 'The people in your life',
  interests: 'Hobbies, tastes and what you follow',
  other: 'Everything else',
  [UNSORTED]: 'Not given a topic yet — Consolidate sorts them',
};

/** Core memories first — they are always in context — then the rest. */
function coreFirst(items: MemoryItem[]): MemoryItem[] {
  return [...items.filter((m) => m.kind === 'core'), ...items.filter((m) => m.kind !== 'core')];
}

function topicLabel(topic: string): string {
  return topic.charAt(0).toUpperCase() + topic.slice(1);
}

/** The `?topic=` a memory is listed under. */
function topicKey(m: MemoryItem): string {
  return m.topic ?? UNSORTED;
}

type Editor = {mode: 'add'} | {mode: 'edit'; item: MemoryItem};

/**
 * The memory page: with no `topic`, a card per topic; with one, that topic's
 * memories, which is where they are added, edited and deleted.
 */
export function MemoryView({topic}: {topic: string | null}) {
  // Suspends on first load and throws on failure — the /memory route wraps this
  // in a QueryBoundary, which supplies the retry fetchKey.
  const fetchKey = useQueryRetry();
  const memoryData = useLazyLoadQuery<TMemoriesQuery>(
    memoriesQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey},
  );

  const [actionError, setActionError] = useState<string | null>(null);
  const [editor, setEditor] = useState<Editor | null>(null);
  const [text, setText] = useState('');
  const [kind, setKind] = useState<MemoryKind>('fact');
  const [topicDraft, setTopicDraft] = useState('');
  const [deleteTarget, setDeleteTarget] = useState<MemoryItem | null>(null);

  const topicNames: readonly string[] = memoryData.memoryTopics;

  function closeEditor() {
    setEditor(null);
    setActionError(null);
  }

  function openAdd() {
    setText('');
    setKind('fact');
    // Added from a topic's page, it goes in that topic.
    setTopicDraft(topic && topicNames.includes(topic) ? topic : '');
    setActionError(null);
    setEditor({mode: 'add'});
  }

  function openEdit(m: MemoryItem) {
    setText(m.text);
    setTopicDraft(m.topic ?? '');
    setActionError(null);
    setEditor({mode: 'edit', item: m});
  }

  // Each write leaves the Relay store correct via its own updater (or, for an
  // edit, via plain normalization), so none of these refetch the list.
  const addAction = useAsyncAction(() => commitAddMemory(text.trim(), kind, topicDraft || null), {
    onSuccess: closeEditor,
    onError: (e) => setActionError(e.message),
  });

  const updateAction = useAsyncAction(
    (id: string, t: string, to: string) =>
      commitUpdateMemoryItem(id, t, undefined, to || undefined),
    {
      onSuccess: closeEditor,
      onError: (e) => setActionError(e.message),
    },
  );

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

  // The exception: consolidation and sorting rewrite the set server-side, so
  // there is no delta to apply — re-read the list into the store.
  const consolidateAction = useAsyncAction(
    async () => {
      await commitConsolidateMemory();
      await commitSortMemoryTopics();
      await refreshMemories();
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
    topic: m.topic ?? null,
    updated_at: m.updatedAt,
  }));

  function submitEditor() {
    if (!editor) return;
    if (editor.mode === 'add') void addAction.run();
    else void updateAction.run(editor.item.id, text.trim(), topicDraft);
  }

  const consolidateButton = (
    <button
      {...stylex.props(btn.base)}
      onClick={() => void consolidateAction.run()}
      disabled={consolidateAction.pending}
      title="Pull new memories out of recent conversations, and sort any without a topic"
    >
      {consolidateAction.pending ? 'Consolidating…' : 'Consolidate'}
    </button>
  );
  const addButton = (
    <button {...stylex.props(btn.base, btn.primary)} onClick={openAdd}>
      <PlusIcon size={14} /> Add memory
    </button>
  );

  const editorOpen = editor !== null;
  const editKind = editor?.mode === 'edit' ? editor.item.kind : kind;

  return (
    <div {...stylex.props(page.scroll)}>
      {topic === null ? (
        <TopicList
          all={all}
          topicNames={topicNames}
          actions={
            <>
              {consolidateButton}
              {addButton}
            </>
          }
          onAdd={openAdd}
        />
      ) : (
        <TopicDetail
          topic={topic}
          items={all.filter((m) => topicKey(m) === topic)}
          actions={addButton}
          onEdit={openEdit}
          onDelete={setDeleteTarget}
        />
      )}

      {actionError && !editorOpen && <div {...stylex.props(page.error)}>{actionError}</div>}

      <FormModal
        open={editorOpen}
        title={editor?.mode === 'edit' ? 'Edit memory' : 'Add memory'}
        subtitle="One fact per item, as a short fragment — “Lives in Austin”, not “The user lives in Austin”."
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
            placeholder="e.g. Prefers answers in Spanish when discussing travel"
          />
        </div>
        <label {...stylex.props(field.group)}>
          <span {...stylex.props(field.label)}>Topic</span>
          <select
            {...stylex.props(field.select, field.selectChrome)}
            value={topicDraft}
            onChange={(e) => setTopicDraft(e.target.value)}
          >
            <option value="" disabled={editor?.mode === 'edit' && editor.item.topic !== null}>
              {editor?.mode === 'edit' ? 'Unsorted' : 'Let the agent sort it'}
            </option>
            {topicNames.map((t) => (
              <option key={t} value={t}>
                {topicLabel(t)} — {TOPIC_HINTS[t] ?? ''}
              </option>
            ))}
          </select>
        </label>
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

/** The index: a card per topic that has memories, unsorted ones last. */
function TopicList({
  all,
  topicNames,
  actions,
  onAdd,
}: {
  all: MemoryItem[];
  topicNames: readonly string[];
  actions: React.ReactNode;
  onAdd: () => void;
}) {
  const groups = [...topicNames, UNSORTED]
    .map((t) => ({topic: t, items: all.filter((m) => topicKey(m) === t)}))
    .filter((g) => g.items.length > 0);

  return (
    <>
      <PageHeader
        title="Memory"
        subtitle="What the agent remembers about you across conversations, by topic."
        details={
          <>
            <strong>Core</strong> items go into every conversation, grouped under their topic.{' '}
            <strong>Facts</strong> are brought in when they are relevant to what you ask. The agent
            saves new items as it learns, and <strong>Consolidate</strong> pulls more out of recent
            conversations and sorts any that have no topic yet.
          </>
        }
        actions={actions}
      />

      {groups.length === 0 ? (
        <div {...stylex.props(page.empty)}>
          <p>No memories yet.</p>
          <p>
            Add one, ask the agent to <code>remember</code> something, or run consolidation after
            some conversation history accumulates.
          </p>
          <button {...stylex.props(btn.base, btn.primary, styles.emptyBtn)} onClick={onAdd}>
            <PlusIcon size={14} /> Add memory
          </button>
        </div>
      ) : (
        <ul {...stylex.props(topics.grid)}>
          {groups.map(({topic, items}) => {
            const core = items.filter((m) => m.kind === 'core').length;
            return (
              <li key={topic}>
                <Link
                  to="/memory"
                  search={{topic}}
                  {...stylex.props(topics.card, topic === UNSORTED && topics.cardUnsorted)}
                >
                  <span {...stylex.props(topics.head)}>
                    <span {...stylex.props(topics.name)}>{topicLabel(topic)}</span>
                    <span {...stylex.props(page.count)}>{items.length}</span>
                  </span>
                  <span {...stylex.props(topics.hint)}>{TOPIC_HINTS[topic] ?? ''}</span>
                  <span {...stylex.props(topics.preview)}>
                    {coreFirst(items)
                      .slice(0, 3)
                      .map((m) => m.text)
                      .join(' · ')}
                  </span>
                  {core > 0 && (
                    <span {...stylex.props(topics.core)}>
                      <span {...stylex.props(kindDot.base, kindDot.core)} />
                      {core} always in context
                    </span>
                  )}
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </>
  );
}

/** One topic's memories: core ones first, as they are always in context. */
function TopicDetail({
  topic,
  items,
  actions,
  onEdit,
  onDelete,
}: {
  topic: string;
  items: MemoryItem[];
  actions: React.ReactNode;
  onEdit: (m: MemoryItem) => void;
  onDelete: (m: MemoryItem) => void;
}) {
  const ordered = coreFirst(items);
  return (
    <>
      <header {...stylex.props(page.header)}>
        <div {...stylex.props(page.headerMain)}>
          <nav {...stylex.props(detail.breadcrumb)}>
            <Link to="/memory" search={{}} {...stylex.props(detail.breadcrumbLink)}>
              Memory
            </Link>{' '}
            /
          </nav>
          <h1 {...stylex.props(page.title)}>{topicLabel(topic)}</h1>
          <p {...stylex.props(page.subtitle)}>{TOPIC_HINTS[topic] ?? ''}</p>
        </div>
        <div {...stylex.props(page.headerActions)}>{actions}</div>
      </header>

      {ordered.length === 0 ? (
        <div {...stylex.props(page.empty)}>
          <p>Nothing under {topicLabel(topic)} yet.</p>
        </div>
      ) : (
        <ul {...stylex.props(page.list)}>
          {ordered.map((m) => (
            <li key={m.id} {...stylex.props(item.root)}>
              <div {...stylex.props(item.main)}>
                <span {...stylex.props(item.text)}>{m.text}</span>
                <span {...stylex.props(item.meta, topics.itemMeta)}>
                  {m.kind === 'core' && (
                    <>
                      <span {...stylex.props(kindDot.base, kindDot.core)} />
                      Always in context ·{' '}
                    </>
                  )}
                  Updated <TimeAgo iso={m.updated_at} />
                </span>
              </div>
              <div {...stylex.props(item.actions)}>
                <button
                  {...stylex.props(iconBtn.base)}
                  title="Edit memory"
                  onClick={() => onEdit(m)}
                >
                  <EditIcon size={14} />
                </button>
                <button
                  {...stylex.props(iconBtn.base, iconBtn.danger)}
                  title="Delete memory"
                  onClick={() => onDelete(m)}
                >
                  <TrashIcon size={14} />
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </>
  );
}

const styles = stylex.create({
  /** The empty state's call to action sits a little clear of the prose. */
  emptyBtn: {marginBlockStart: 8},
});
