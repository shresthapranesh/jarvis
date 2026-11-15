import * as stylex from '@stylexjs/stylex';
import {useState} from 'react';

import {useAsyncAction} from '../hooks/useAsyncAction';
import {refreshPendingApprovals} from '../hooks/usePendingApprovals';
import {useToast} from '../lib/toast';
import type {PendingApproval} from '../lib/types';
import {commitResolveApproval} from '../relay/ResolveApprovalMutation';
import {answer as answerStyles, card} from './ApprovalControls.styles';

/**
 * The answer to one request: its arguments, and approve/deny or a reply.
 * It goes inside a card that already says what is asking and what it asks.
 */
export function ApprovalControls({
  approval,
  onResolved,
}: {
  approval: PendingApproval;
  onResolved?: () => void;
}) {
  const toast = useToast();
  const [answer, setAnswer] = useState('');
  const [showArgs, setShowArgs] = useState(false);

  const action = useAsyncAction(
    async (text: string) => {
      const outcome = await commitResolveApproval(approval.id, text);
      // A deferred approval *does* something when granted, so report what —
      // "approved" alone leaves you wondering whether the delete actually ran.
      if (approval.deferred && outcome.result) {
        toast.push(`${outcome.status}: ${outcome.result}`, 'success');
      }
      await refreshPendingApprovals();
    },
    {
      onSuccess: () => {
        setAnswer('');
        onResolved?.();
      },
      onError: (e) => toast.push(e.message, 'error'),
    },
  );

  const isGate = approval.kind === 'approval';

  return (
    <>
      {approval.args_json && (
        <div>
          <button
            {...stylex.props(card.argsToggle)}
            type="button"
            onClick={() => setShowArgs((v) => !v)}
          >
            {showArgs ? 'Hide' : 'Show'} arguments
          </button>
          {showArgs && <pre {...stylex.props(card.argsPre)}>{approval.args_json}</pre>}
        </div>
      )}

      <div {...stylex.props(answerStyles.row)}>
        {isGate ? (
          <>
            <button
              {...stylex.props(answerStyles.btn, answerStyles.approve)}
              type="button"
              disabled={action.pending}
              onClick={() => void action.run('approve')}
            >
              Approve
            </button>
            <button
              {...stylex.props(answerStyles.btn, answerStyles.deny)}
              type="button"
              disabled={action.pending}
              onClick={() => void action.run('deny')}
            >
              Deny
            </button>
          </>
        ) : (
          <>
            <input
              {...stylex.props(answerStyles.input)}
              value={answer}
              placeholder="Your answer…"
              disabled={action.pending}
              onChange={(e) => setAnswer(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && answer.trim()) void action.run(answer.trim());
              }}
            />
            <button
              {...stylex.props(answerStyles.btn, answerStyles.approve)}
              type="button"
              disabled={action.pending || !answer.trim()}
              onClick={() => void action.run(answer.trim())}
            >
              Send
            </button>
          </>
        )}
      </div>
    </>
  );
}
