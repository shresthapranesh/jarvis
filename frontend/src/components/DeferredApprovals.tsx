import * as stylex from '@stylexjs/stylex';

import {useAsyncAction} from '../hooks/useAsyncAction';
import {refreshPendingApprovals, usePendingApprovals} from '../hooks/usePendingApprovals';
import {useToast} from '../lib/toast';
import type {PendingApproval} from '../lib/types';
import {commitResolveApproval} from '../relay/ResolveApprovalMutation';
import {channels, colors, type} from '../theme/tokens.stylex';

/**
 * Deferred approvals raised by *this* conversation, shown where they were asked
 * for rather than only in `/approvals`.
 *
 * Read from the polled inbox, not from the run's event stream, and that is the
 * whole point of the shape. A deferred request blocks nothing — the agent's
 * delete was recorded instead of performed and the run finished normally — so
 * the moment the user is most likely to look, the run (and its `TaskState`) is
 * already gone. Sourcing this from the durable rows means it survives the run
 * ending, a reload, and a second tab; the `approval_request` event only nudges
 * the poll (`useTaskEvents`) so it appears immediately instead of up to a tick
 * later.
 *
 * `InterruptPrompt` stays separate on purpose: it takes free text because the
 * question may be one, and it means a run is suspended right now. This is a
 * yes/no on work that has already been skipped.
 */
export function DeferredApprovals({conversationId}: {conversationId: string}) {
  const pending = usePendingApprovals();
  const mine = pending.filter((a) => a.deferred && a.parent_id === conversationId);
  if (mine.length === 0) return null;
  return (
    <div {...stylex.props(styles.wrap)}>
      {mine.map((a) => (
        <DeferredCard key={a.id} approval={a} />
      ))}
    </div>
  );
}

function DeferredCard({approval}: {approval: PendingApproval}) {
  const toast = useToast();
  const action = useAsyncAction(
    async (answer: string) => {
      const outcome = await commitResolveApproval(approval.id, answer);
      // Approving a deferred request is what *runs* it, so say what happened —
      // "approved" alone leaves you wondering whether the delete went through.
      toast.push(
        outcome.result ? `${outcome.status}: ${outcome.result}` : outcome.status,
        outcome.status === 'approved' ? 'success' : 'info',
      );
      await refreshPendingApprovals();
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  return (
    <div {...stylex.props(styles.card)}>
      <div {...stylex.props(styles.header)}>
        <svg
          width="14"
          height="14"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M12 9v4" />
          <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0Z" />
          <path d="M12 17h.01" />
        </svg>
        <span>{approval.label || 'Needs approval'}</span>
        <span {...stylex.props(styles.tag)}>not run yet</span>
      </div>
      <div {...stylex.props(styles.question)}>{approval.question}</div>
      <div {...stylex.props(styles.footer)}>
        <span {...stylex.props(styles.hint)}>Runs only if you approve it.</span>
        <button
          type="button"
          {...stylex.props(styles.btn)}
          disabled={action.pending}
          onClick={() => action.run('deny')}
        >
          Deny
        </button>
        <button
          type="button"
          {...stylex.props(styles.btn, styles.approve)}
          disabled={action.pending}
          onClick={() => action.run('approve')}
        >
          {action.pending ? 'Working…' : 'Approve & run'}
        </button>
      </div>
    </div>
  );
}

const styles = stylex.create({
  wrap: {
    maxWidth: 760,
    marginBlock: '0 10px',
    marginInline: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: 8,
  },
  card: {
    backgroundColor: colors.warningBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.warningBorder,
    borderRadius: 3,
    display: 'flex',
    flexDirection: 'column',
    paddingBlock: 12,
    paddingInline: 14,
    gap: 8,
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 6,
    fontSize: type.tSmall,
    color: colors.warningText,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.04em',
  },
  tag: {
    color: colors.textDim,
    fontWeight: 400,
    textTransform: 'none',
    letterSpacing: 'normal',
    fontSize: type.tMicro,
  },
  question: {color: colors.text, fontSize: type.tBody, lineHeight: 1.45, whiteSpace: 'pre-wrap'},
  footer: {display: 'flex', alignItems: 'center', gap: 8},
  hint: {flex: 1, fontSize: type.tMicro, color: colors.textDim},
  btn: {
    backgroundColor: colors.surface,
    color: colors.text,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    paddingBlock: 6,
    paddingInline: 12,
    fontSize: type.tUi,
    fontWeight: 600,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.45},
  },
  approve: {
    backgroundColor: colors.warn,
    borderColor: colors.warn,
    color: colors.accentContrast,
    boxShadow: {
      default: null,
      ':hover:not(:disabled)': `0 2px 12px rgba(${channels.warn}, 0.35)`,
    },
  },
});
