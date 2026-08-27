import * as stylex from '@stylexjs/stylex';
import {createFileRoute, Link, useNavigate} from '@tanstack/react-router';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ConversationListQuery} from '../__generated__/ConversationListQuery.graphql';
import {InputBox} from '../components/InputBox';
import {errorBubble, page} from '../components/ui';
import {useRunningTasks} from '../hooks/useRunningTasks';
import {greeting, relativeTime} from '../lib/format';
import type {MediaAttachment} from '../lib/types';
import {uploadStagedAttachment} from '../lib/uploads';
import {conversationListQuery} from '../relay/ConversationListQuery';
import {decodeGlobalId} from '../relay/globalId';
import {commitStartTask} from '../relay/StartTaskMutation';
import {dispatch, recent as recentStyles, run} from './index.styles';

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

  const running = useRunningTasks();

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
    <div {...stylex.props(page.root)}>
      <div {...stylex.props(dispatch.scroll)}>
        <div {...stylex.props(dispatch.inner)}>
          <header {...stylex.props(dispatch.head)}>
            <h1 {...stylex.props(dispatch.greeting)}>{greeting()}</h1>
            <p {...stylex.props(dispatch.sub)}>
              {running.length > 0 ? (
                <>
                  <span {...stylex.props(dispatch.pulse)} aria-hidden="true" />
                  {running.length} {running.length === 1 ? 'run' : 'runs'} in flight
                </>
              ) : (
                'Nothing running. Ask for something below.'
              )}
            </p>
          </header>

          {error && <div {...stylex.props(errorBubble.base, dispatch.error)}>{error}</div>}

          <div>
            <InputBox
              onSubmit={handleSubmit}
              disabled={loading}
              fullWidth
              incognito={incognito}
              onToggleIncognito={() => setIncognito((v) => !v)}
            />
          </div>

          {running.length > 0 && (
            <section {...stylex.props(dispatch.section)}>
              <h2 {...stylex.props(dispatch.heading)}>In flight</h2>
              <ul {...stylex.props(recentStyles.list)}>
                {running.map((task) => (
                  <li {...stylex.props(run.row)} key={task.id}>
                    <span {...stylex.props(run.mark)} aria-hidden="true" />
                    <span {...stylex.props(run.label)}>{task.label}</span>
                    <span {...stylex.props(run.time)}>{task.kind}</span>
                    <span {...stylex.props(run.time)}>{relativeTime(task.started_at)}</span>
                  </li>
                ))}
              </ul>
              <Link to="/tasks" {...stylex.props(dispatch.more)}>
                All tasks →
              </Link>
            </section>
          )}

          {recent.length > 0 && (
            <section {...stylex.props(dispatch.section)}>
              <h2 {...stylex.props(dispatch.heading)}>Recent</h2>
              <ul {...stylex.props(recentStyles.list)}>
                {recent.map((conv) => (
                  <li key={conv.id}>
                    <Link to="/c/$id" params={{id: conv.id}} {...stylex.props(recentStyles.row)}>
                      <span {...stylex.props(recentStyles.title)}>{conv.title}</span>
                      <span {...stylex.props(recentStyles.meta)}>
                        {conv.messageCount} {conv.messageCount === 1 ? 'message' : 'messages'}
                        <span {...stylex.props(recentStyles.dot)} aria-hidden="true">
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
