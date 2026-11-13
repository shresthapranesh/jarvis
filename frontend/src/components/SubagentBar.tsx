import * as stylex from '@stylexjs/stylex';
import {Link} from '@tanstack/react-router';
import {useState} from 'react';

import {colors, type} from '../theme/tokens.stylex';
import {worker} from './ui';

export interface SubagentInfo {
  /** The sub-agent's conversation — its page is `/c/<conversationId>`. */
  conversationId: string;
  role: string;
  title: string | null;
  /** `pending`, `running`, `done`, `error` or `stopped` — its latest turn's. */
  status: string;
  result: string | null;
}

const RESULT_PREVIEW = 160;

const DOT: Record<string, keyof typeof worker> = {
  pending: 'dotRunning',
  running: 'dotRunning',
  done: 'dotDone',
  error: 'dotError',
};

const STATUS_LABEL: Record<string, string> = {
  pending: 'Queued',
  running: 'Running',
  done: 'Done',
  error: 'Failed',
  stopped: 'Stopped',
};

export function isLive(s: SubagentInfo): boolean {
  return s.status === 'running' || s.status === 'pending';
}

function summary(subagents: SubagentInfo[]): string {
  const live = subagents.filter(isLive).length;
  const failed = subagents.filter((s) => s.status === 'error').length;
  const noun = `sub-agent${subagents.length !== 1 ? 's' : ''}`;
  if (live > 0) return `${live} of ${subagents.length} ${noun} running`;
  if (failed > 0) return `${subagents.length} ${noun} · ${failed} failed`;
  return `${subagents.length} ${noun} finished`;
}

/**
 * The conversation's sub-agents and where each one stands, above the
 * composer. It belongs to the conversation, not to a reply: a sub-agent
 * outlives the turn that spawned it — it is given follow-ups later, and one
 * left running in the background finishes between turns.
 *
 * Each row opens the sub-agent's own conversation, where its whole transcript
 * is and where it can be written to directly.
 */
export function SubagentBar({subagents}: {subagents: SubagentInfo[]}) {
  const anyLive = subagents.some(isLive);
  // Open while something is running; the reader's own toggle wins after that.
  const [toggled, setToggled] = useState<boolean | null>(null);
  if (subagents.length === 0) return null;
  const open = toggled ?? anyLive;

  return (
    <div {...stylex.props(styles.wrap)}>
      <button
        type="button"
        {...stylex.props(styles.header)}
        onClick={() => setToggled(!open)}
        aria-expanded={open}
      >
        <span {...stylex.props(worker.dot, anyLive ? worker.dotRunning : worker.dotDone)} />
        <span>{summary(subagents)}</span>
        <span {...stylex.props(styles.chevron, open && styles.chevronOpen)} aria-hidden="true">
          ›
        </span>
      </button>
      {open && (
        <ul {...stylex.props(styles.list)}>
          {subagents.map((s) => (
            <li key={s.conversationId} {...stylex.props(styles.item)}>
              <Link
                to="/c/$id"
                params={{id: s.conversationId}}
                search={{}}
                {...stylex.props(styles.row)}
                title="Open this sub-agent's conversation"
              >
                <span {...stylex.props(worker.dot, worker[DOT[s.status] ?? 'dotUnknown'])} />
                <span {...stylex.props(worker.role)}>{s.role}</span>
                <span {...stylex.props(worker.task, styles.task)}>
                  {s.title ?? 'Untitled task'}
                </span>
                <span {...stylex.props(styles.status, s.status === 'error' && styles.statusError)}>
                  {STATUS_LABEL[s.status] ?? s.status}
                </span>
              </Link>
              {!isLive(s) && s.result && (
                <div {...stylex.props(styles.result)}>
                  {s.result.length > RESULT_PREVIEW
                    ? s.result.slice(0, RESULT_PREVIEW) + '…'
                    : s.result}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

const styles = stylex.create({
  wrap: {
    maxWidth: 760,
    marginBlock: '0 8px',
    marginInline: 'auto',
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  },
  header: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    alignSelf: 'flex-start',
    borderWidth: 0,
    backgroundColor: 'transparent',
    paddingBlock: 2,
    paddingInline: 2,
    cursor: 'pointer',
    fontFamily: 'inherit',
    fontSize: type.tSmall,
    color: {default: colors.textDim, ':hover': colors.text},
  },
  chevron: {display: 'inline-block', transition: 'transform 0.15s ease'},
  chevronOpen: {transform: 'rotate(90deg)'},
  list: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 2,
    maxHeight: 220,
    overflowY: 'auto',
  },
  item: {
    borderRadius: 3,
    backgroundColor: colors.surface,
    display: 'flex',
    flexDirection: 'column',
  },
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    minWidth: 0,
    paddingBlock: 6,
    paddingInline: 10,
    textDecoration: 'none',
    borderRadius: 3,
    backgroundColor: {default: 'transparent', ':hover': colors.surface},
  },
  task: {flex: 1, minWidth: 0},
  status: {flex: 'none', fontSize: type.tSmall, color: colors.textDim},
  statusError: {color: '#d06a5c'},
  result: {
    fontSize: type.tSmall,
    lineHeight: 1.5,
    color: colors.textDim,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    paddingBlockEnd: 7,
    paddingInline: '24px 10px',
  },
});
