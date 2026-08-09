import {useQuery} from '@tanstack/react-query';
import {createFileRoute, Link, useNavigate} from '@tanstack/react-router';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ConversationListQuery} from '../__generated__/ConversationListQuery.graphql';
import {InputBox} from '../components/InputBox';
import {greeting, relativeTime} from '../lib/format';
import type {MediaAttachment, RunningTask} from '../lib/types';
import {uploadStagedAttachment} from '../lib/uploads';
import {conversationListQuery} from '../relay/ConversationListQuery';
import {decodeGlobalId} from '../relay/globalId';
import {fetchRunningTasks} from '../relay/RunningTasksQuery';
import {commitStartTask} from '../relay/StartTaskMutation';

export const Route = createFileRoute('/')({component: IndexPage});

const RECENT_LIMIT = 5;

function IndexPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [incognito, setIncognito] = useState(false);

  const convData = useLazyLoadQuery<ConversationListQuery>(
    conversationListQuery,
    {},
    {fetchPolicy: 'store-and-network'},
  );

  const recent = useMemo(
    () =>
      convData.conversations.slice(0, RECENT_LIMIT).map((c) => ({
        id: decodeGlobalId(c.id),
        title: c.title ?? 'Untitled',
        createdAt: c.createdAt as string,
        messageCount: c.messageCount as number,
      })),
    [convData.conversations],
  );

  const {data: runningTasks} = useQuery({
    queryKey: ['running-tasks'],
    queryFn: fetchRunningTasks,
    refetchInterval: (query) =>
      ((query.state.data as RunningTask[] | undefined)?.length ?? 0) > 0 ? 2000 : false,
  });
  const running = runningTasks ?? [];

  async function handleSubmit(query: string, model: string, attachments: MediaAttachment[]) {
    setLoading(true);
    setError(null);
    try {
      const uploads = attachments.length
        ? await Promise.all(
            attachments.map(async (a) => ({uploadId: (await uploadStagedAttachment(a)).uploadId})),
          )
        : null;
      const {taskId, conversationId} = await commitStartTask({
        input: {query, model, attachmentUploads: uploads, ephemeral: incognito},
      });

      // The /c/$id route loader will fetch the new conversation page (which
      // includes the user msg + running assistant msg the mutation just created);
      // useLazyLoadQuery on the page renders from the warmed Relay store on
      // first paint, no client-side pre-seed needed.
      await navigate({
        to: '/c/$id',
        params: {id: conversationId},
        search: {task: taskId},
      });
    } catch (err) {
      setError((err as Error).message);
      setLoading(false);
    }
  }

  return (
    <div className="page dispatch">
      <div className="dispatch-scroll">
        <div className="dispatch-inner">
          <header className="dispatch-head">
            <h1 className="dispatch-greeting">{greeting()}</h1>
            <p className="dispatch-sub">
              {running.length > 0 ? (
                <>
                  <span className="dispatch-pulse" aria-hidden="true" />
                  {running.length} {running.length === 1 ? 'run' : 'runs'} in flight
                </>
              ) : (
                'Nothing running. Ask for something below.'
              )}
            </p>
          </header>

          {error && <div className="error-bubble dispatch-error">{error}</div>}

          <div className="dispatch-composer">
            <InputBox
              onSubmit={handleSubmit}
              disabled={loading}
              incognito={incognito}
              onToggleIncognito={() => setIncognito((v) => !v)}
            />
          </div>

          {running.length > 0 && (
            <section className="dispatch-section">
              <h2 className="dispatch-section-heading">In flight</h2>
              <ul className="dispatch-runs">
                {running.map((task) => (
                  <li className="dispatch-run" key={task.id}>
                    <span className="dispatch-run-mark" aria-hidden="true" />
                    <span className="dispatch-run-label">{task.label}</span>
                    <span className="dispatch-run-kind">{task.kind}</span>
                    <span className="dispatch-run-time">{relativeTime(task.started_at)}</span>
                  </li>
                ))}
              </ul>
              <Link to="/tasks" className="dispatch-more">
                All tasks →
              </Link>
            </section>
          )}

          {recent.length > 0 && (
            <section className="dispatch-section">
              <h2 className="dispatch-section-heading">Recent</h2>
              <ul className="dispatch-recent">
                {recent.map((conv) => (
                  <li key={conv.id}>
                    <Link to="/c/$id" params={{id: conv.id}} className="dispatch-recent-row">
                      <span className="dispatch-recent-title">{conv.title}</span>
                      <span className="dispatch-recent-meta">
                        {conv.messageCount} {conv.messageCount === 1 ? 'message' : 'messages'}
                        <span className="dispatch-dot" aria-hidden="true">
                          ·
                        </span>
                        {relativeTime(conv.createdAt)}
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </section>
          )}
        </div>
      </div>
    </div>
  );
}
