import * as stylex from '@stylexjs/stylex';

import type {Message} from '../lib/types';
import {channels, colors, type} from '../theme/tokens.stylex';
import {turn} from './ui';

interface Props {
  messages: Message[];
  /** Withdraw a queued message. Absent while the run has no id to withdraw against. */
  onUnqueue?: (messageId: string) => void;
}

/**
 * Messages the user sent while the run was still going.
 *
 * They render *below* the streaming turn, which is where they belong: the
 * answer above is still being written for the earlier prompt, and putting a
 * not-yet-seen message above it would read as one the agent had already been
 * given. The dashed rule and the "queued" label say the same thing — this is
 * waiting, not sent.
 *
 * A queued row is a real user message (status `queued`), so once the run
 * delivers it, the row flips to `done` and it moves into the thread proper on
 * the next refetch. Nothing here has to hand it over.
 */
export function QueuedMessages({messages, onUnqueue}: Props) {
  if (messages.length === 0) return null;
  return (
    <div {...stylex.props(turn.base, styles.group)}>
      <div {...stylex.props(styles.label)}>
        {messages.length === 1 ? 'Queued' : `Queued · ${messages.length}`}
        <span {...stylex.props(styles.labelHint)}>sent when the run reaches its next step</span>
      </div>
      {messages.map((msg) => (
        <div key={msg.id} {...stylex.props(styles.row)}>
          <div {...stylex.props(styles.bubble)}>{msg.content}</div>
          {onUnqueue && (
            <button
              type="button"
              {...stylex.props(styles.withdraw)}
              title="Remove from queue"
              aria-label="Remove from queue"
              onClick={() => onUnqueue(msg.id)}
            >
              ✕
            </button>
          )}
        </div>
      ))}
    </div>
  );
}

const styles = stylex.create({
  group: {gap: 6},
  label: {
    alignSelf: 'flex-end',
    display: 'flex',
    alignItems: 'baseline',
    gap: 8,
    fontSize: type.tMicro,
    fontWeight: 600,
    letterSpacing: '0.06em',
    textTransform: 'uppercase',
    color: colors.textFaint,
  },
  labelHint: {
    fontWeight: 400,
    letterSpacing: 0,
    textTransform: 'none',
    opacity: 0.8,
  },
  row: {
    alignSelf: 'flex-end',
    display: 'flex',
    alignItems: 'flex-start',
    gap: 6,
    maxWidth: '71%',
    // The button sits outside the bubble and only firms up on hover, so a
    // queue of several does not read as a column of ✕.
    '--queued-withdraw-opacity': {default: '0.35', ':hover': '1'},
  },
  bubble: {
    backgroundColor: 'transparent',
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.border,
    borderRadius: 2,
    paddingBlock: 8,
    paddingInline: 12,
    lineHeight: 1.6,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: type.tBody,
    color: colors.textDim,
  },
  withdraw: {
    flex: 'none',
    marginBlockStart: 6,
    backgroundColor: 'transparent',
    borderStyle: 'none',
    padding: 2,
    cursor: 'pointer',
    fontSize: type.tMicro,
    lineHeight: 1,
    color: `rgba(${channels.tint}, 0.7)`,
    opacity: 'var(--queued-withdraw-opacity)',
    transition: 'opacity 0.15s',
  },
});
