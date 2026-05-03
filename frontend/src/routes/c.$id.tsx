import {useSuspenseQuery, useQueryClient} from '@tanstack/react-query';
import {createFileRoute} from '@tanstack/react-router';
import {useEffect, useLayoutEffect, useRef, useState} from 'react';

import {ActivitySidebar} from '../components/ActivitySidebar';
import {ArtifactPanel} from '../components/ArtifactPanel';
import {InputBox} from '../components/InputBox';
import {InterruptPrompt} from '../components/InterruptPrompt';
import {MessageThread} from '../components/MessageThread';
import {useStream} from '../hooks/useStream';
import {fetchConversation, startTask, stopTask} from '../lib/api';
import type {MediaAttachment, Message, Step} from '../lib/types';

export const Route = createFileRoute('/c/$id')({
  loader: ({context, params}) =>
    context.queryClient.ensureQueryData({
      queryKey: ['conversation', params.id],
      queryFn: () => fetchConversation(params.id),
    }),
  component: ConversationPage,
});

function ConversationPage() {
  const {id} = Route.useParams();
  const {data: conv} = useSuspenseQuery({
    queryKey: ['conversation', id],
    queryFn: () => fetchConversation(id),
  });

  // Find a running task in the loaded messages (handles reconnect automatically)
  const runningMsg = conv.messages.find((m) => m.role === 'assistant' && m.status === 'running');
  const {streaming, text, thinkingText, steps, artifacts, error, pendingInterrupt, safetyBlock} = useStream(runningMsg?.id ?? null, id);

  const queryClient = useQueryClient();
  const [pendingUser, setPendingUser] = useState<Message | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Right panel state
  const [panelOpen, setPanelOpen] = useState(false);
  const [panelSteps, setPanelSteps] = useState<Step[]>([]);
  const [artifactPanelOpen, setArtifactPanelOpen] = useState(false);
  const [selectedArtifactId, setSelectedArtifactId] = useState<string | null>(null);

  // Auto-open artifact panel when the agent produces a new artifact mid-stream.
  useEffect(() => {
    if (artifacts.length > 0) {
      const latest = artifacts[artifacts.length - 1];
      setSelectedArtifactId(latest.id);
      setArtifactPanelOpen(true);
    }
  }, [artifacts.length]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keep panel steps current while streaming (only if panel is already open)
  useEffect(() => {
    if ((streaming || !!runningMsg) && panelOpen && steps.length > 0) {
      setPanelSteps(steps);
    }
  }, [streaming, runningMsg, steps, panelOpen]);

  useLayoutEffect(() => {
    bottomRef.current?.scrollIntoView();
  }, [id]);

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
      await startTask(query, model, attachments, id);
      // Reload conversation so the new running message is visible,
      // which triggers useStream to auto-subscribe
      await queryClient.invalidateQueries({queryKey: ['conversation', id]});
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
    ...conv.messages
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
        bottomRef={bottomRef}
        onShowSteps={handleShowSteps}
      />
      {panelOpen && (
        <ActivitySidebar
          steps={panelSteps}
          isLive={isActive}
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
      {!artifactPanelOpen && artifacts.length > 0 && (
        <button
          className="artifact-fab"
          onClick={() => setArtifactPanelOpen(true)}
          type="button"
          title="Show artifacts"
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
            <polyline points="14 2 14 8 20 8" />
          </svg>
          {artifacts.length} artifact{artifacts.length !== 1 ? 's' : ''}
        </button>
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
        />
      </footer>
    </div>
  );
}
