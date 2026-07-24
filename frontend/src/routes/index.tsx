import {createFileRoute, useNavigate} from '@tanstack/react-router';
import {useState} from 'react';

import {InputBox} from '../components/InputBox';
import type {MediaAttachment} from '../lib/types';
import {uploadStagedAttachment} from '../lib/uploads';
import {commitStartTask} from '../relay/StartTaskMutation';

export const Route = createFileRoute('/')({component: IndexPage});

function IndexPage() {
  const navigate = useNavigate();
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [incognito, setIncognito] = useState(false);

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
    <div className="page">
      <div id="messages">
        {error && (
          <div className="turn">
            <div className="error-bubble">⚠ {error}</div>
          </div>
        )}
      </div>
      <footer className="page-footer">
        <InputBox
          onSubmit={handleSubmit}
          disabled={loading}
          incognito={incognito}
          onToggleIncognito={() => setIncognito((v) => !v)}
        />
      </footer>
    </div>
  );
}
