import * as stylex from '@stylexjs/stylex';
import {useEffect, useRef, useState} from 'react';

import {channels, colors, type} from '../theme/tokens.stylex';
import {worker} from './ui';

export interface SubagentInfo {
  /** The sub-agent's conversation — its page is `/c/<conversationId>`. */
  conversationId: string;
  title: string | null;
  /** `pending`, `running`, `done`, `error` or `stopped` — its latest turn's. */
  status: string;
  result: string | null;
}

export const SUBAGENT_DOT: Record<string, keyof typeof worker> = {
  pending: 'dotRunning',
  running: 'dotRunning',
  done: 'dotDone',
  error: 'dotError',
};

export const SUBAGENT_STATUS: Record<string, string> = {
  pending: 'Queued',
  running: 'Working',
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
  const plural = (n: number) => `sub-agent${n !== 1 ? 's' : ''}`;
  if (live > 0) return `${live} ${plural(live)} working`;
  const total = `${subagents.length} ${plural(subagents.length)}`;
  return failed > 0 ? `${total} · ${failed} failed` : total;
}

interface Props {
  subagents: SubagentInfo[];
  /** The sub-agent whose conversation is open beside the thread, if any. */
  openId: string | null;
  onOpen: (conversationId: string) => void;
}

/**
 * The conversation's sub-agents as one chip above the composer: how many are
 * working, and the full list a click away. It reads the conversation, not the
 * run, so it stays for as long as the conversation has sub-agents — a
 * sub-agent outlives the turn that spawned it, and its transcript is still
 * worth opening after everything has finished.
 *
 * A row opens that sub-agent's conversation beside the thread.
 */
export function SubagentChip({subagents, openId, onOpen}: Props) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: PointerEvent) => {
      if (!wrapRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false);
    };
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  if (subagents.length === 0) return null;
  const anyLive = subagents.some(isLive);

  return (
    <div ref={wrapRef} {...stylex.props(styles.wrap)}>
      {open && (
        <ul {...stylex.props(styles.list)} aria-label="Sub-agents">
          {subagents.map((s) => (
            <li key={s.conversationId}>
              <button
                type="button"
                {...stylex.props(styles.row, s.conversationId === openId && styles.rowOpen)}
                onClick={() => {
                  onOpen(s.conversationId);
                  setOpen(false);
                }}
                title="Open this sub-agent's conversation"
              >
                <span
                  {...stylex.props(worker.dot, worker[SUBAGENT_DOT[s.status] ?? 'dotUnknown'])}
                />
                <span {...stylex.props(worker.task, styles.task)}>
                  {s.title ?? 'Untitled task'}
                </span>
                <span {...stylex.props(styles.status, s.status === 'error' && styles.statusError)}>
                  {SUBAGENT_STATUS[s.status] ?? s.status}
                </span>
              </button>
            </li>
          ))}
        </ul>
      )}
      <button
        type="button"
        {...stylex.props(styles.chip, open && styles.chipOpen)}
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        <span {...stylex.props(worker.dot, anyLive ? worker.dotRunning : worker.dotDone)} />
        <span>{summary(subagents)}</span>
        <span {...stylex.props(styles.chevron, open && styles.chevronOpen)} aria-hidden="true">
          ›
        </span>
      </button>
    </div>
  );
}

const styles = stylex.create({
  // The composer's measure, so the chip sits on its leading edge.
  wrap: {
    position: 'relative',
    maxWidth: {default: 740, '@media (max-width: 860px)': 'none'},
    marginBlock: '0 8px',
    marginInline: 'auto',
    display: 'flex',
  },
  chip: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    borderWidth: 0,
    borderRadius: 999,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    paddingBlock: 4,
    paddingInline: '10px 9px',
    cursor: 'pointer',
    fontFamily: 'inherit',
    fontSize: type.tSmall,
    color: {default: colors.textDim, ':hover': colors.text},
    transition: 'color 0.15s, background-color 0.15s',
  },
  chipOpen: {backgroundColor: colors.surface2, color: colors.text},
  // Points up when closed — the list opens above the chip.
  chevron: {
    display: 'inline-block',
    transform: 'rotate(-90deg)',
    transition: 'transform 0.15s ease',
  },
  chevronOpen: {transform: 'rotate(90deg)'},
  list: {
    position: 'absolute',
    insetBlockEnd: 'calc(100% + 6px)',
    insetInlineStart: 0,
    width: 'min(460px, 100%)',
    maxHeight: 280,
    overflowY: 'auto',
    listStyle: 'none',
    margin: 0,
    padding: 4,
    display: 'flex',
    flexDirection: 'column',
    gap: 1,
    borderRadius: 6,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.glassBorder,
    // The opaque panel stock: `surface` is a 3.5% tint, and the thread would
    // read straight through it.
    backgroundColor: colors.glassBg,
    boxShadow: `0 8px 28px rgba(${channels.shadow}, 0.4)`,
    zIndex: 20,
  },
  row: {
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    width: '100%',
    minWidth: 0,
    paddingBlock: 7,
    paddingInline: 9,
    borderWidth: 0,
    borderRadius: 4,
    textAlign: 'left',
    fontFamily: 'inherit',
    cursor: 'pointer',
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
  },
  rowOpen: {backgroundColor: colors.surface2},
  task: {flex: 1, minWidth: 0},
  status: {flex: 'none', fontSize: type.tSmall, color: colors.textDim},
  statusError: {color: '#d06a5c'},
});
