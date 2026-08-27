import * as stylex from '@stylexjs/stylex';
import {createFileRoute, useNavigate} from '@tanstack/react-router';
import {useState} from 'react';

import {kindBadge, kindBadgeStyle, page} from '../components/ui';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {
  refreshPendingApprovals,
  usePendingApprovals,
  usePendingApprovalsLoaded,
} from '../hooks/usePendingApprovals';
import {formatRelativeTime} from '../lib/api';
import {useToast} from '../lib/toast';
import type {ApprovalSource, PendingApproval} from '../lib/types';
import {commitResolveApproval} from '../relay/ResolveApprovalMutation';
// `counts` and `answer` are also local variable names in this file.
import {answer as answerStyles, card, counts as countStyles} from './approvals.styles';

export const Route = createFileRoute('/approvals')({
  component: ApprovalsPage,
});

const SOURCE_LABEL: Record<ApprovalSource, string> = {
  chat: 'Chat',
  workflow: 'Workflow',
  automation: 'Automation',
  board_task: 'Board',
};

function ApprovalCard({approval}: {approval: PendingApproval}) {
  const navigate = useNavigate();
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
      onSuccess: () => setAnswer(''),
      onError: (e) => toast.push(e.message, 'error'),
    },
  );

  function open() {
    if (approval.source === 'board_task') {
      void navigate({to: '/board'});
    } else if (approval.source === 'workflow' && approval.parent_id) {
      void navigate({to: '/workflow/$id', params: {id: approval.parent_id}});
    } else if (approval.source === 'automation') {
      void navigate({to: '/automation'});
    } else if (approval.parent_id) {
      void navigate({to: '/c/$id', params: {id: approval.parent_id}});
    }
  }

  const isGate = approval.kind === 'approval';
  // A deferred request may have no origin to open: the SDK identifies its
  // conversation, but an automation or CLI caller has none to identify.
  const canOpen =
    approval.source === 'board_task' || approval.source === 'automation' || !!approval.parent_id;

  return (
    <li {...stylex.props(card.root)}>
      <div {...stylex.props(card.head)}>
        <span {...stylex.props(kindBadge.base, kindBadgeStyle(approval.source))}>
          {SOURCE_LABEL[approval.source] ?? approval.source}
        </span>
        <span {...stylex.props(card.origin)} title={approval.label}>
          {approval.label}
        </span>
        {approval.tool && <code {...stylex.props(card.tool)}>{approval.tool}</code>}
        {approval.deferred && (
          <span
            {...stylex.props(card.deferred)}
            title="Nothing is blocked — approving is what runs it"
          >
            runs on approval
          </span>
        )}
        <span {...stylex.props(card.age)}>{formatRelativeTime(approval.requested_at)}</span>
        {canOpen && (
          <button {...stylex.props(card.open)} type="button" onClick={open}>
            Open
          </button>
        )}
      </div>

      <p {...stylex.props(card.question)}>{approval.question}</p>

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
    </li>
  );
}

function ApprovalsPage() {
  const approvals = usePendingApprovals();
  const loaded = usePendingApprovalsLoaded();

  const counts = approvals.reduce<Record<string, number>>((acc, a) => {
    acc[a.source] = (acc[a.source] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <div {...stylex.props(page.scroll, styles.page)}>
      <header {...stylex.props(styles.header)}>
        <h1 {...stylex.props(page.title)}>Approvals</h1>
        <p {...stylex.props(page.subtitle, styles.subtitle)}>
          Everything waiting on a human answer. Requests are durable and survive a restart — but a
          suspended run may not: workflow pauses are marked expired at startup and drop off this
          list, while “runs on approval” items block nothing and wait indefinitely.
        </p>
        {approvals.length > 0 && (
          <div {...stylex.props(countStyles.row)}>
            <span {...stylex.props(countStyles.total)}>{approvals.length} pending</span>
            {Object.entries(counts).map(([source, n]) => (
              <span key={source} {...stylex.props(countStyles.chip)}>
                {SOURCE_LABEL[source as ApprovalSource] ?? source} · {n}
              </span>
            ))}
          </div>
        )}
      </header>

      {!loaded ? (
        <div {...stylex.props(page.empty)}>Loading…</div>
      ) : approvals.length === 0 ? (
        <div {...stylex.props(page.empty)}>Nothing is waiting on you.</div>
      ) : (
        <ul {...stylex.props(card.list)}>
          {approvals.map((a) => (
            <ApprovalCard key={a.id} approval={a} />
          ))}
        </ul>
      )}
    </div>
  );
}

const styles = stylex.create({
  /** The header carries its own bottom margin, so the flex gap is off. */
  page: {gap: 0},
  header: {marginBlockEnd: 24},
  subtitle: {maxWidth: 540},
});
