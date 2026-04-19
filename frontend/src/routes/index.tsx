import {createFileRoute, useNavigate} from '@tanstack/react-router';
import {useState} from 'react';

import {InputBox} from '../components/InputBox';
import {startTask} from '../lib/api';
import type {MediaAttachment} from '../lib/types';

export const Route = createFileRoute('/')({component: IndexPage});

function IndexPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit(query: string, model: string, attachments: MediaAttachment[]) {
    setLoading(true);
    setError(null);
    try {
      const {conversation_id} = await startTask(query, model, attachments);
      await navigate({to: '/c/$id', params: {id: conversation_id}});
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
