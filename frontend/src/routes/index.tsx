import * as stylex from '@stylexjs/stylex';
import {createFileRoute, Link, useNavigate} from '@tanstack/react-router';
import {useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {ConversationListQuery} from '../__generated__/ConversationListQuery.graphql';
import {BoltIcon, KanbanIcon, WorkflowIcon} from '../components/icons';
import {InputBox} from '../components/InputBox';
import {errorBubble, page, TimeAgo} from '../components/ui';
import {useRunningTasks} from '../hooks/useRunningTasks';
import {greeting} from '../lib/format';
import {runLink} from '../lib/runLink';
import type {UploadedFile} from '../lib/uploads';
import {conversationListQuery} from '../relay/ConversationListQuery';
import {decodeGlobalId} from '../relay/globalId';
import {commitStartTask} from '../relay/StartTaskMutation';
import {dispatch, recent as recentStyles, run, start} from './index.styles';

export const Route = createFileRoute('/')({component: IndexPage});

const RECENT_LIMIT = 5;

// The three ways to hand work off rather than chat about it, each said as
// the moment you would reach for it.
const STARTING_POINTS = [
  {
    to: '/activity',
    Icon: KanbanIcon,
    title: 'Queue a task',
    line: 'Add it to Activity and the agent works through it on its own.',
  },
  {
    to: '/automation',
    Icon: BoltIcon,
    title: 'Schedule it',
    line: 'Run a prompt on a schedule, or when a webhook calls.',
  },
  {
    to: '/workflow',
    Icon: WorkflowIcon,
    title: 'Build a workflow',
    line: 'Chain agents and approvals into steps you can run again.',
  },
] as const;

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
        lastActiveAt: c.lastActiveAt as string,
        messageCount: c.messageCount as number,
      })),
    [convData.conversations],
  );

  const running = useRunningTasks();

  async function handleSubmit(
    query: string,
    model: string,
    effort: string | null,
    attachments: UploadedFile[],
  ) {
    setLoading(true);
    setError(null);
    try {
      const {taskId, conversationId} = await commitStartTask({
        input: {
          query,
          model,
          effort,
          ephemeral: incognito,
          attachmentUploads: attachments.map((a) => ({uploadId: a.uploadId})),
        },
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

          <nav {...stylex.props(start.grid)} aria-label="Other ways to start work">
            {STARTING_POINTS.map(({to, Icon, title, line}) => (
              <Link key={to} to={to} {...stylex.props(start.card)}>
                <span {...stylex.props(start.title)}>
                  <Icon size={14} />
                  {title}
                </span>
                <span {...stylex.props(start.line)}>{line}</span>
              </Link>
            ))}
          </nav>

          {running.length > 0 && (
            <section {...stylex.props(dispatch.section)}>
              <h2 {...stylex.props(dispatch.heading)}>In flight</h2>
              <ul {...stylex.props(recentStyles.list)}>
                {running.map((task) => {
                  const link = runLink(task);
                  const body = (
                    <>
                      <span {...stylex.props(run.mark)} aria-hidden="true" />
                      <span {...stylex.props(run.label)}>{task.label}</span>
                      <span {...stylex.props(run.time)}>{task.kind.replace('_', ' ')}</span>
                      <span {...stylex.props(run.time)}>
                        <TimeAgo iso={task.started_at} compact />
                      </span>
                    </>
                  );
                  return (
                    <li key={task.id}>
                      {link ? (
                        <Link {...link} {...stylex.props(run.row, run.rowLink)}>
                          {body}
                        </Link>
                      ) : (
                        <div {...stylex.props(run.row)}>{body}</div>
                      )}
                    </li>
                  );
                })}
              </ul>
              <Link to="/activity" {...stylex.props(dispatch.more)}>
                All activity →
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
                        <TimeAgo iso={conv.lastActiveAt} compact />
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
