import {useQuery, useSuspenseInfiniteQuery, useQueryClient} from '@tanstack/react-query';
import {createFileRoute, useNavigate} from '@tanstack/react-router';
import {useEffect, useLayoutEffect, useMemo, useRef, useState} from 'react';

import {ActivitySidebar} from '../components/ActivitySidebar';
import {ArtifactPanel} from '../components/ArtifactPanel';
import {InputBox} from '../components/InputBox';
import {InterruptPrompt} from '../components/InterruptPrompt';
import {MessageThread} from '../components/MessageThread';
import {useStream} from '../hooks/useStream';
import {deleteDocument, fetchConversationPage, listArtifacts, listDocuments, listTodos, refetchConversationFirstPage, startTask, stopTask} from '../lib/api';
import type {MediaAttachment, Message, MessagePage, Step} from '../lib/types';

const CONVERSATION_QUERY_OPTIONS = (id: string) => ({
  queryKey: ['conversation', id] as const,
  queryFn: ({pageParam}: {pageParam: string | undefined}) =>
    fetchConversationPage(id, pageParam),
  initialPageParam: undefined as string | undefined,
  // Cursor for the next (older) page = created_at of the oldest message in the
  // most recently fetched page. Backend returns each page oldest-first, so the
  // first message of the page is the oldest.
  getNextPageParam: (last: MessagePage) =>
    last.has_more ? last.messages[0]?.created_at : undefined,
});

export const Route = createFileRoute('/c/$id')({
  validateSearch: (search: Record<string, unknown>): {task?: string} =>
    typeof search.task === 'string' ? {task: search.task} : {},
  loader: ({context, params}) =>
    context.queryClient.ensureInfiniteQueryData(CONVERSATION_QUERY_OPTIONS(params.id)),
  component: ConversationPage,
});

function ConversationPage() {
  const {id} = Route.useParams();
  const {task: searchTaskId} = Route.useSearch();
  const navigate = useNavigate();
  const {data, hasNextPage, isFetchingNextPage, fetchNextPage} = useSuspenseInfiniteQuery(
    CONVERSATION_QUERY_OPTIONS(id),
  );

  // Newest page is fetched first (pages[0]); older pages append at higher indices.
  // For chronological display we want oldest first → reverse pages, then flatten.
  const allMessages = useMemo<Message[]>(
    () => data.pages.slice().reverse().flatMap((p) => p.messages),
    [data.pages],
  );

  const runningMsg = allMessages.find((m) => m.role === 'assistant' && m.status === 'running');

  // Track the just-started task id locally so useStream can subscribe immediately
  // — without waiting for the paginated cache refetch to surface the running
  // assistant message. Falls back to the cache once the cache catches up.
  const [pendingTaskId, setPendingTaskId] = useState<string | null>(searchTaskId ?? null);
  const streamTaskId = pendingTaskId ?? runningMsg?.id ?? null;

  const {streaming, text, thinkingText, steps, artifacts, todos: liveTodos, error, pendingInterrupt, safetyBlock} =
    useStream(streamTaskId, id);

  const queryClient = useQueryClient();
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

  const [panelOpen, setPanelOpen] = useState(false);
  const [panelSteps, setPanelSteps] = useState<Step[]>([]);
  const [artifactPanelOpen, setArtifactPanelOpen] = useState(false);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);

  // Persisted artifacts — keeps the FAB available after reload / on past
  // conversations, even when the live-stream `artifacts` array is empty.
  // useStream invalidates ['artifacts', id] on stream done, so this stays
  // in sync without extra wiring.
  const {data: persistedArtifacts = []} = useQuery({
    queryKey: ['artifacts', id],
    queryFn: () => listArtifacts(id),
  });
  const totalArtifactCount = Math.max(persistedArtifacts.length, artifacts.length);

  const {data: persistedDocuments = []} = useQuery({
    queryKey: ['documents', id],
    queryFn: () => listDocuments(id),
  });

  const {data: persistedTodos = []} = useQuery({
    queryKey: ['todos', id],
    queryFn: () => listTodos(id),
  });

  const todos = liveTodos ?? persistedTodos;

  const conversationModel = data.pages[0]?.model;

  async function handleDeletePersistedDocument(docId: string) {
    try {
      await deleteDocument(docId);
      await queryClient.invalidateQueries({queryKey: ['documents', id]});
    } catch (err) {
      console.error('Failed to delete document:', err);
    }
  }

  useEffect(() => {
    if (artifacts.length > 0) {
      const latest = artifacts[artifacts.length - 1];
      setSelectedArtifactId(latest.id);
      setArtifactPanelOpen(true);
    }
  }, [artifacts.length]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if ((streaming || !!runningMsg) && panelOpen && steps.length > 0) {
      setPanelSteps(steps);
    }
  }, [streaming, runningMsg, steps, panelOpen]);

  useLayoutEffect(() => {
    bottomRef.current?.scrollIntoView();
  }, [id]);

  // Infinite-scroll-upward: when the top sentinel becomes visible, fetch the
  // next (older) page. Capture scrollHeight before the fetch so we can adjust
  // scrollTop afterwards — this keeps whatever row the user was looking at
  // pinned in place instead of jumping up by the height of the inserted rows.
  useEffect(() => {
    const sentinel = topRef.current;
    const container = containerRef.current;
    if (!sentinel || !container || !hasNextPage) return;

    const obs = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (!entry?.isIntersecting || isFetchingNextPage) return;
        const prevHeight = container.scrollHeight;
        const prevTop = container.scrollTop;
        void fetchNextPage().then(() => {
          // `scroll-behavior: smooth` is set on #messages; bypass it explicitly
          // so the scrollTop adjustment is instant (otherwise the user sees
          // the page animate after older messages are prepended).
          requestAnimationFrame(() => {
            container.scrollTo({
              top: prevTop + (container.scrollHeight - prevHeight),
              behavior: 'instant' as ScrollBehavior,
            });
          });
        });
      },
      {root: container, threshold: 0, rootMargin: '200px 0px 0px 0px'},
    );
    obs.observe(sentinel);
    return () => obs.disconnect();
  }, [hasNextPage, isFetchingNextPage, fetchNextPage]);

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
      created_at: new Date().toISOString(),
      steps: [],
    });
    bottomRef.current?.scrollIntoView({behavior: 'smooth'});
    try {
      const {task_id} = await startTask(query, model, attachments, id);
      // Subscribe to the live stream immediately — the paginated cache may
      // take a moment to surface the new running message via refetch.
      setPendingTaskId(task_id);
      // Only the most-recent page changed (new user msg + new running assistant
      // msg). Trim cached pages to page 0 then refetch — keeps the user's
      // scrolled-up history out of an unnecessary refetch.
      await refetchConversationFirstPage(queryClient, id);
      void queryClient.invalidateQueries({queryKey: ['running-tasks']});
      if (attachments.some((a) => a.type === 'document')) {
        void queryClient.invalidateQueries({queryKey: ['documents', id]});
      }
    } finally {
      setPendingUser(null);
    }
  }

  async function handleStop() {
    if (runningMsg) {
      try {
        await stopTask(runningMsg.id);
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

  return (
    <div className="page">
      <MessageThread
        messages={messages}
        streamingText={isActive ? text : undefined}
        streamingThinkingText={isActive ? thinkingText : undefined}
        streamingSteps={isActive ? steps : undefined}
        isStreaming={isActive}
        streamError={error ?? undefined}
        streamSafetyBlock={isActive ? safetyBlock : null}
        todos={todos}
        bottomRef={bottomRef}
        topRef={topRef}
        containerRef={containerRef}
        isLoadingOlder={isFetchingNextPage}
        onShowSteps={handleShowSteps}
      />
      {panelOpen && (
        <ActivitySidebar
          steps={panelSteps}
          isLive={isActive}
          todos={todos}
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
      <footer className="page-footer">
        {pendingInterrupt && runningMsg && (
          <InterruptPrompt
            taskId={runningMsg.id}
            question={pendingInterrupt.question}
          />
        )}
        <InputBox
          onSubmit={handleSubmit}
          disabled={isActive}
          onStop={handleStop}
          artifactCount={totalArtifactCount}
          artifactPanelOpen={artifactPanelOpen}
          onToggleArtifacts={() => setArtifactPanelOpen((v) => !v)}
          conversationId={id}
          initialModel={conversationModel}
          persistedDocuments={persistedDocuments}
          onDeletePersistedDocument={handleDeletePersistedDocument}
        />
      </footer>
    </div>
  );
}
