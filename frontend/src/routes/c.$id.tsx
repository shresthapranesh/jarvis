import * as stylex from '@stylexjs/stylex';
import {createFileRoute, Link, useNavigate} from '@tanstack/react-router';
import {useEffect, useLayoutEffect, useMemo, useRef, useState} from 'react';
import {useLazyLoadQuery, usePaginationFragment} from 'react-relay';

import type {ArtifactListQuery} from '../__generated__/ArtifactListQuery.graphql';
import type {ConversationPageFragment$key} from '../__generated__/ConversationPageFragment.graphql';
import type {ConversationPageQuery as TConversationPageQuery} from '../__generated__/ConversationPageQuery.graphql';
import type {ConversationPageRefetchQuery} from '../__generated__/ConversationPageRefetchQuery.graphql';
import type {DocumentListQuery} from '../__generated__/DocumentListQuery.graphql';
import type {TodoListQuery} from '../__generated__/TodoListQuery.graphql';
import {ActivitySidebar} from '../components/ActivitySidebar';
import {ArtifactPanel} from '../components/ArtifactPanel';
import {FolderIcon} from '../components/icons';
import {InputBox} from '../components/InputBox';
import {InterruptPrompt} from '../components/InterruptPrompt';
import {MessageThread} from '../components/MessageThread';
import {RunSpine} from '../components/RunSpine';
import {page} from '../components/ui';
import {refreshRunningTasks} from '../hooks/useRunningTasks';
import {useTaskEvents} from '../hooks/useTaskEvents';
import type {
  ArtifactCard,
  MediaAttachment,
  Message,
  PersistedDocument,
  Step,
  TodoItem,
  TodoStatus,
} from '../lib/types';
import {mapMessage} from '../lib/types';
import {uploadStagedAttachment} from '../lib/uploads';
import {artifactListQuery} from '../relay/ArtifactListQuery';
import {conversationPageFragment} from '../relay/ConversationPageFragment';
import {
  CONVERSATION_PAGE_SIZE,
  conversationPageQuery,
  loadConversationPage,
} from '../relay/ConversationPageQuery';
import {commitDeleteDocument} from '../relay/DeleteDocumentMutation';
import {commitDiscardConversation} from '../relay/DiscardConversationMutation';
import {documentListQuery, refreshDocumentList} from '../relay/DocumentListQuery';
import {decodeGlobalId, encodeGlobalId} from '../relay/globalId';
import {commitStartTask} from '../relay/StartTaskMutation';
import {commitStopTask} from '../relay/StopTaskMutation';
import {todoListQuery} from '../relay/TodoListQuery';
import {conv} from './c.$id.styles';

export const Route = createFileRoute('/c/$id')({
  validateSearch: (search: Record<string, unknown>): {task?: string} =>
    typeof search.task === 'string' ? {task: search.task} : {},
  loader: ({params}) => loadConversationPage(params.id),
  component: ConversationPage,
});

function ConversationPage() {
  const {id} = Route.useParams();
  const {task: searchTaskId} = Route.useSearch();
  const navigate = useNavigate();

  const queryData = useLazyLoadQuery<TConversationPageQuery>(
    conversationPageQuery,
    {id: encodeGlobalId('Conversation', id), count: CONVERSATION_PAGE_SIZE, cursor: null},
    {fetchPolicy: 'store-or-network'},
  );
  if (!queryData.conversation) throw new Error('Conversation not found');

  const {data, loadPrevious, hasPrevious, isLoadingPrevious} = usePaginationFragment<
    ConversationPageRefetchQuery,
    ConversationPageFragment$key
  >(conversationPageFragment, queryData.conversation);

  // Connection edges arrive oldest-first within each page, and loadPrevious
  // prepends older pages — so the flattened edge list is already chronological.
  const allMessages = useMemo<Message[]>(
    () => data.messages.edges.map((e) => mapMessage(e.node)),
    [data.messages.edges],
  );

  const runningMsg = allMessages.find((m) => m.role === 'assistant' && m.status === 'running');

  // Track the just-started task id locally so useStream can subscribe immediately
  // — without waiting for the paginated cache refetch to surface the running
  // assistant message. Falls back to the cache once the cache catches up.
  const [pendingTaskId, setPendingTaskId] = useState<string | null>(searchTaskId ?? null);
  const streamTaskId = pendingTaskId ?? runningMsg?.id ?? null;

  const {
    streaming,
    text,
    thinkingText,
    steps,
    workers,
    artifacts,
    todos: liveTodos,
    error,
    pendingInterrupt,
    budget: liveBudget,
    perf: livePerf,
  } = useTaskEvents(streamTaskId, id) as any;

  const [pendingUser, setPendingUser] = useState<Message | null>(null);

  // Once the paginated cache has the running/done message with this id, the
  // cache is authoritative and we no longer need the local fallback. Also
  // strip ?task=… from the URL once it has served its purpose.
  useEffect(() => {
    if (!pendingTaskId) return;
    if (allMessages.some((m) => m.id === pendingTaskId)) {
      setPendingTaskId(null);
      if (searchTaskId) {
        void navigate({to: '/c/$id', params: {id}, search: {}, replace: true});
      }
    }
  }, [pendingTaskId, allMessages, searchTaskId, id, navigate]);
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const topRef = useRef<HTMLDivElement | null>(null);
  const containerRef = useRef<HTMLDivElement | null>(null);
  // Whether the thread should stay glued to the bottom as content grows. True
  // only during the settle window right after a conversation opens — see the
  // scroll-restore effects below.
  const pinBottomRef = useRef(true);

  const [panelOpen, setPanelOpen] = useState(false);
  const [panelSteps, setPanelSteps] = useState<Step[]>([]);
  const [artifactPanelOpen, setArtifactPanelOpen] = useState(false);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);

  // Persisted artifacts — keeps the FAB available after reload / on past
  // conversations, even when the live-stream `artifacts` array is empty.
  // useTaskEvents calls refreshArtifactList on stream done, keeping this fresh.
  const artifactListData = useLazyLoadQuery<ArtifactListQuery>(
    artifactListQuery,
    {conversationId: id},
    {fetchPolicy: 'store-and-network'},
  );
  const totalArtifactCount = Math.max(artifactListData.artifacts.length, artifacts.length);

  // Artifacts render under the assistant message that produced them, keyed by
  // the `messageId` the write stamps (tools/artifacts.py). Rows predating that
  // stamp — and anything written outside a chat turn — carry no message, so
  // they fall back to the newest assistant message rather than becoming
  // unreachable: the card is the only way into the side panel.
  const artifactsByMessage = useMemo(() => {
    const lastAssistantId = [...allMessages].reverse().find((m) => m.role === 'assistant')?.id;
    const byMessage = new Map<string, ArtifactCard[]>();
    for (const a of artifactListData.artifacts) {
      const owner = a.messageId ?? lastAssistantId;
      if (!owner) continue;
      const card: ArtifactCard = {
        id: decodeGlobalId(a.id),
        title: a.title,
        kind: a.kind,
        mimeType: a.mimeType,
      };
      const bucket = byMessage.get(owner);
      if (bucket) bucket.push(card);
      else byMessage.set(owner, [card]);
    }
    return byMessage;
  }, [artifactListData.artifacts, allMessages]);

  // Mid-run the assistant message is still a placeholder, so the live refs
  // carry the cards until the stream finishes and the list query refetches.
  const streamingArtifacts = useMemo<ArtifactCard[]>(
    () =>
      artifacts.map((a: any) => ({
        id: a.id,
        title: a.title,
        kind: a.kind ?? 'markdown',
        action: a.action,
      })),
    [artifacts],
  );

  function handleOpenArtifact(artifactId: string) {
    setSelectedArtifactId(artifactId);
    setArtifactPanelOpen(true);
  }

  const documentListData = useLazyLoadQuery<DocumentListQuery>(
    documentListQuery,
    {conversationId: id},
    {fetchPolicy: 'store-and-network'},
  );
  const persistedDocuments = useMemo<PersistedDocument[]>(
    () =>
      documentListData.documents.map((d) => ({
        id: decodeGlobalId(d.id),
        conversation_id: d.conversationId,
        message_id: d.messageId ?? null,
        filename: d.filename,
        mime_type: d.mimeType,
        size: d.size,
        created_at: d.createdAt,
      })),
    [documentListData.documents],
  );

  const todoListData = useLazyLoadQuery<TodoListQuery>(
    todoListQuery,
    {conversationId: id},
    {fetchPolicy: 'store-and-network'},
  );
  const persistedTodos = useMemo<TodoItem[]>(
    () => todoListData.todos.map((t) => ({text: t.text, status: t.status as TodoStatus})),
    [todoListData.todos],
  );

  const todos = liveTodos ?? persistedTodos;

  const conversationModel = data.model;

  async function handleDeletePersistedDocument(docId: string) {
    try {
      await commitDeleteDocument(docId);
      await refreshDocumentList(id);
    } catch (err) {
      console.error('Failed to delete document:', err);
    }
  }

  useEffect(() => {
    if ((streaming || !!runningMsg) && panelOpen && steps.length > 0) {
      setPanelSteps(steps);
    }
  }, [streaming, runningMsg, steps, panelOpen]);

  // Open a conversation already at the bottom. Assigning scrollTop directly (in
  // a layout effect, so it lands before paint) means the first frame the user
  // sees is the newest message — never a scroll down through the history.
  useLayoutEffect(() => {
    pinBottomRef.current = true;
    const container = containerRef.current;
    if (container) container.scrollTop = container.scrollHeight;
  }, [id]);

  // …and hold it there while the page settles. The restore above runs on the
  // first commit, before markdown, code blocks and images have their final
  // height, so a restored thread keeps growing underneath it and the newest
  // message ends up short of the fold.
  //
  // The observers watch the children, not #messages itself: the container is a
  // flex child with its own scrollbar, so growing content changes its
  // scrollHeight but never its border box — a ResizeObserver on the container
  // would never fire.
  //
  // The window is deliberately short-lived. It ends at the first scroll away
  // from the bottom, and at SETTLE_MS regardless, so this stays a fix for
  // late-arriving layout and never becomes follow-the-output-forever: an
  // already-running conversation opened in a second tab must not start yanking
  // itself down on every token, and handleSubmit's smooth scroll must not get
  // cut short by an instant re-pin landing on top of it.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const SETTLE_MS = 1500;
    const BOTTOM_SLACK = 4; // fractional scrollHeight/clientHeight never sum exactly

    const repin = () => {
      if (pinBottomRef.current) container.scrollTop = container.scrollHeight;
    };

    // Every re-pin lands exactly at the bottom, so a scroll that settles
    // anywhere else came from the reader — or from the older-page prepend
    // below, which restores an earlier position on purpose. Either way the
    // scroll position is theirs from then on.
    const onScroll = () => {
      if (!pinBottomRef.current) return;
      const fromBottom = container.scrollHeight - container.clientHeight - container.scrollTop;
      if (fromBottom > BOTTOM_SLACK) pinBottomRef.current = false;
    };

    const resize = new ResizeObserver(repin);
    for (const child of Array.from(container.children)) resize.observe(child);

    // Children mount as the page hydrates; each new one needs observing too.
    const mutation = new MutationObserver((records) => {
      for (const record of records) {
        for (const node of Array.from(record.addedNodes)) {
          if (node instanceof Element) resize.observe(node);
        }
      }
      repin();
    });
    mutation.observe(container, {childList: true});

    container.addEventListener('scroll', onScroll, {passive: true});
    const settled = window.setTimeout(() => {
      pinBottomRef.current = false;
    }, SETTLE_MS);

    return () => {
      resize.disconnect();
      mutation.disconnect();
      container.removeEventListener('scroll', onScroll);
      window.clearTimeout(settled);
    };
  }, [id]);

  // Infinite-scroll-upward: when the top sentinel becomes visible, load the
  // previous (older) page via Relay's pagination fragment. Capture scrollHeight
  // before the fetch so we can adjust scrollTop afterwards — keeps whatever
  // row the user was looking at pinned in place instead of jumping up by the
  // height of the inserted rows.
  useEffect(() => {
    const sentinel = topRef.current;
    const container = containerRef.current;
    if (!sentinel || !container || !hasPrevious) return;

    const obs = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (!entry?.isIntersecting || isLoadingPrevious) return;
        const prevHeight = container.scrollHeight;
        const prevTop = container.scrollTop;
        // Release the bottom pin before the rows land. If the settle window is
        // still open (short first page — the top sentinel can be in view from
        // the start), the resize from prepending would otherwise re-pin to the
        // bottom, and it fires before the fixup below gets its frame.
        pinBottomRef.current = false;
        loadPrevious(CONVERSATION_PAGE_SIZE, {
          onComplete: () => {
            requestAnimationFrame(() => {
              container.scrollTop = prevTop + (container.scrollHeight - prevHeight);
            });
          },
        });
      },
      {root: container, threshold: 0, rootMargin: '200px 0px 0px 0px'},
    );
    obs.observe(sentinel);
    return () => obs.disconnect();
  }, [hasPrevious, isLoadingPrevious, loadPrevious]);

  function handleShowSteps(s: Step[]) {
    setPanelSteps(s);
    setPanelOpen(true);
  }

  async function handleSubmit(query: string, model: string, attachments: MediaAttachment[]) {
    setPendingUser({
      id: 'pending',
      role: 'user',
      content: query,
      model: null,
      status: 'done',
      input_tokens: null,
      output_tokens: null,
      ttft_ms: null,
      llm_ms: null,
      prefill_tps: null,
      eval_tps: null,
      created_at: new Date().toISOString(),
      steps: [],
    });
    // Submitting takes over the scroll: this animation owns the trip to the
    // bottom, so an in-flight settle window must not jump ahead of it.
    pinBottomRef.current = false;
    bottomRef.current?.scrollIntoView({behavior: 'smooth'});
    try {
      const uploads = attachments.length
        ? await Promise.all(
            attachments.map(async (a) => ({uploadId: (await uploadStagedAttachment(a)).uploadId})),
          )
        : null;
      const {taskId} = await commitStartTask({
        input: {query, model, conversationId: id, attachmentUploads: uploads},
      });
      // Subscribe to the live stream immediately — the connection won't surface
      // the new running message until the refetch below completes.
      setPendingTaskId(taskId);
      // Refetch the newest page so the user msg + running assistant rows land
      // in the Relay store; usePaginationFragment re-renders automatically.
      await loadConversationPage(id);
      void refreshRunningTasks();
      if (attachments.some((a) => a.type === 'document')) {
        void refreshDocumentList(id);
      }
    } finally {
      setPendingUser(null);
    }
  }

  async function handleStop() {
    if (runningMsg) {
      try {
        // runningMsg.id is the raw message UUID (== taskId for assistant
        // messages) — mapMessage() decodes the Relay GlobalID for us.
        await commitStopTask(runningMsg.id);
      } catch (err) {
        console.error('Failed to stop task:', err);
      }
    }
  }

  // Build displayed messages: replace running assistant message with streaming state
  const messages: Message[] = [
    ...allMessages
      .filter((m) => !(m.role === 'assistant' && m.status === 'running'))
      .concat(pendingUser ? [pendingUser] : []),
  ];

  const isActive = streaming || !!runningMsg;
  const isEphemeral = data.ephemeral;

  // The spine is the always-on, collapsed form of the activity sidebar: live
  // steps while a run is in flight, the last completed run's trace at rest.
  // It yields the rail whenever a full panel is open so they never stack.
  const spineSteps = useMemo<Step[]>(() => {
    if (isActive) return steps;
    for (let i = allMessages.length - 1; i >= 0; i--) {
      const m = allMessages[i];
      if (m.role === 'assistant' && m.steps.length > 0) return m.steps;
    }
    return [];
  }, [isActive, steps, allMessages]);

  const showSpine = !panelOpen && !artifactPanelOpen && (isActive || spineSteps.length > 0);

  // Incognito teardown. Discard when the user closes the tab (best-effort) and
  // when they navigate away from a settled ephemeral chat. `activeRef` keeps the
  // unmount cleanup from deleting a conversation whose run is still in flight —
  // the startup sweep + the next explicit close cover that case instead.
  const activeRef = useRef(isActive);
  activeRef.current = isActive;
  useEffect(() => {
    if (!isEphemeral) return;
    const onBeforeUnload = () => {
      void commitDiscardConversation(id).catch(() => {});
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => {
      window.removeEventListener('beforeunload', onBeforeUnload);
      if (!activeRef.current) {
        void commitDiscardConversation(id).catch(() => {});
      }
    };
  }, [id, isEphemeral]);

  return (
    <div {...stylex.props(page.root)}>
      {isEphemeral && (
        <div
          {...stylex.props(conv.incognitoBar)}
          title="This conversation is not saved and will be deleted when you leave."
        >
          <svg
            width="12"
            height="12"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
            <line x1="1" y1="1" x2="23" y2="23" />
          </svg>
          Incognito — this chat isn’t saved to history or long-term memory, and is deleted when you
          leave.
        </div>
      )}
      {data.project && (
        <div {...stylex.props(conv.projectBar)}>
          <Link
            to="/projects/$id"
            params={{id: decodeGlobalId(data.project.id)}}
            {...stylex.props(conv.projectBadge)}
            title="Open project"
          >
            <FolderIcon size={12} /> {data.project.name}
          </Link>
        </div>
      )}
      <MessageThread
        messages={messages}
        streamingText={isActive ? text : undefined}
        streamingThinkingText={isActive ? thinkingText : undefined}
        streamingSteps={isActive ? steps : undefined}
        streamingWorkers={isActive ? workers : undefined}
        isStreaming={isActive}
        streamError={error ?? undefined}
        todos={todos}
        bottomRef={bottomRef}
        topRef={topRef}
        containerRef={containerRef}
        isLoadingOlder={isLoadingPrevious}
        onShowSteps={handleShowSteps}
        artifactsByMessage={artifactsByMessage}
        streamingArtifacts={isActive ? streamingArtifacts : undefined}
        onOpenArtifact={handleOpenArtifact}
        openArtifactId={artifactPanelOpen ? selectedArtifactId : null}
        hasSpine={showSpine}
      />
      {showSpine && (
        <RunSpine
          steps={spineSteps}
          workers={isActive ? workers : []}
          artifactCount={totalArtifactCount}
          isLive={isActive}
          budget={liveBudget}
          onExpand={() => handleShowSteps(spineSteps)}
        />
      )}
      {panelOpen && (
        <ActivitySidebar
          steps={panelSteps}
          isLive={isActive}
          todos={todos}
          budget={liveBudget}
          perf={livePerf}
          onClose={() => setPanelOpen(false)}
        />
      )}
      {artifactPanelOpen && (
        <ArtifactPanel
          conversationId={id}
          selectedId={selectedArtifactId}
          onSelect={setSelectedArtifactId}
          onClose={() => setArtifactPanelOpen(false)}
        />
      )}
      <footer {...stylex.props(conv.footer, showSpine && conv.footerWithSpine)}>
        {pendingInterrupt && runningMsg && (
          <InterruptPrompt
            taskId={runningMsg.id}
            question={pendingInterrupt.question}
            approvalId={pendingInterrupt.approvalId}
          />
        )}
        <InputBox
          onSubmit={handleSubmit}
          disabled={isActive}
          onStop={handleStop}
          conversationId={id}
          initialModel={conversationModel}
          persistedDocuments={persistedDocuments}
          onDeletePersistedDocument={handleDeletePersistedDocument}
        />
      </footer>
    </div>
  );
}
