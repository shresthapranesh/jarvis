import {useQueryClient, type InfiniteData} from '@tanstack/react-query';
import {createFileRoute, useNavigate} from '@tanstack/react-router';
import {useState} from 'react';

import {InputBox} from '../components/InputBox';
import {startTask} from '../lib/api';
import type {MediaAttachment, MessagePage} from '../lib/types';

export const Route = createFileRoute('/')({component: IndexPage});

function IndexPage() {
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(query: string, model: string, attachments: MediaAttachment[]) {
    setLoading(true);
    setError(null);
    try {
      const {task_id, conversation_id} = await startTask(query, model, attachments);

      // Pre-seed the conversation cache so /c/$id can render the user message
      // and subscribe to the stream on first paint, without racing the loader
      // prefetch. Real DB rows replace this on the first refetch (stream done).
      const now = new Date();
      queryClient.setQueryData<InfiniteData<MessagePage, string | undefined>>(
        ['conversation', conversation_id],
        {
          pages: [
            {
              id: conversation_id,
              title: query.slice(0, 60),
              model,
              created_at: now.toISOString(),
              messages: [
                {
                  id: `optimistic-user-${conversation_id}`,
                  role: 'user',
                  content: query,
                  model: null,
                  status: 'done',
                  created_at: now.toISOString(),
                  steps: [],
                },
                {
                  id: task_id,
                  role: 'assistant',
                  content: '',
                  model,
                  status: 'running',
                  created_at: new Date(now.getTime() + 1).toISOString(),
                  steps: [],
                },
              ],
              has_more: false,
            },
          ],
          pageParams: [undefined],
        },
      );

      await navigate({
        to: '/c/$id',
        params: {id: conversation_id},
        search: {task: task_id},
      });
    } catch (err) {
      setError((err as Error).message);
      setLoading(false);
    }
  }

  return (
    <div className="page">
      <div id="messages">
        {error && (
          <div className="turn">
            <div className="error-bubble">⚠ {error}</div>
          </div>
        )}
      </div>
      <footer className="page-footer">
        <InputBox onSubmit={handleSubmit} disabled={loading} />
      </footer>
    </div>
  );
}
