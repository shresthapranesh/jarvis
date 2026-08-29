import * as stylex from '@stylexjs/stylex';

import type {TodoItem} from '../lib/types';
import {kf} from '../theme/keyframes.stylex';
import {colors, type} from '../theme/tokens.stylex';
import {stream} from './ui';

interface Props {
  todos: TodoItem[];
  compact?: boolean;
}

const STATUS_GLYPH: Record<TodoItem['status'], string> = {
  pending: '○',
  in_progress: '◐',
  done: '●',
};

export function TodoList({todos, compact = false}: Props) {
  if (!todos || todos.length === 0) return null;

  const doneCount = todos.filter((t) => t.status === 'done').length;
  const inProgress = todos.findIndex((t) => t.status === 'in_progress');
  const pct = todos.length ? Math.round((doneCount / todos.length) * 100) : 0;

  return (
    <div {...stylex.props(styles.card, compact && styles.cardCompact)}>
      <div {...stylex.props(styles.header)}>
        <span {...stylex.props(styles.title)}>
          📋 Plan {inProgress >= 0 ? `· step ${inProgress + 1}` : ''}
        </span>
        <span {...stylex.props(styles.progress)}>
          {doneCount}/{todos.length}
        </span>
      </div>
      {!compact && (
        <div {...stylex.props(styles.bar)}>
          <div
            {...stylex.props(styles.barFill, pct === 100 && styles.barFillComplete)}
            style={{width: `${pct}%`}}
          />
        </div>
      )}
      <ul {...stylex.props(styles.list)}>
        {todos.map((t, i) => (
          <li
            key={i}
            {...stylex.props(
              styles.item,
              // Steps still queued behind the current one recede, but only once
              // there *is* a current one.
              t.status === 'pending' && inProgress !== -1 && i > inProgress && styles.itemUpcoming,
            )}
          >
            <span
              {...stylex.props(
                styles.glyph,
                t.status === 'in_progress' && styles.glyphActive,
                t.status === 'done' && styles.glyphDone,
              )}
              aria-hidden
            >
              {STATUS_GLYPH[t.status]}
            </span>
            <span {...stylex.props(styles.text, t.status === 'done' && styles.textDone)}>
              {t.text}
            </span>
            {t.status === 'in_progress' && (
              <span {...stylex.props(stream.liveDot, styles.liveDotGap)} />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function PlanningEmpty({reason}: {reason?: string}) {
  return (
    <div {...stylex.props(styles.planning)}>
      <div>🧠 Planning… {reason || 'Complex task detected, generating plan'}</div>
      <div {...stylex.props(styles.bar, styles.planningBar)}>
        <div {...stylex.props(styles.planningBarFill)} />
      </div>
    </div>
  );
}

const styles = stylex.create({
  card: {
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    backgroundColor: colors.surface2,
    borderRadius: 3,
    paddingBlock: 12,
    paddingInline: 14,
    marginBlock: 4,
    fontSize: type.tBody,
  },
  cardCompact: {
    marginBlock: '0 12px',
    marginInline: 0,
    paddingBlock: 10,
    paddingInline: 12,
    borderRadius: 3,
    fontSize: type.tUi,
  },

  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    marginBlockEnd: 8,
  },
  title: {fontWeight: 600, color: colors.text, letterSpacing: '0.02em'},
  progress: {fontSize: type.tSmall, color: colors.textDim, fontVariantNumeric: 'tabular-nums'},

  bar: {
    height: 3,
    backgroundColor: colors.surface2,
    borderRadius: 2,
    overflow: 'hidden',
    marginBlock: '0 8px',
    marginInline: 12,
  },
  // Only the width is inline — it is a live percentage, not a design decision.
  barFill: {height: '100%', backgroundColor: colors.text, transition: 'width 0.3s'},
  barFillComplete: {backgroundColor: colors.accent},

  list: {
    listStyle: 'none',
    margin: 0,
    padding: 0,
    display: 'flex',
    flexDirection: 'column',
    gap: 4,
  },
  item: {display: 'flex', alignItems: 'flex-start', gap: 8, lineHeight: 1.4},
  itemUpcoming: {opacity: 0.7},

  glyph: {
    flexShrink: 0,
    width: '1em',
    textAlign: 'center',
    color: colors.textDim,
    fontFamily: type.mono,
  },
  glyphActive: {color: colors.accent},
  glyphDone: {color: colors.ok},

  text: {flex: 1, color: colors.text, wordBreak: 'break-word'},
  textDone: {
    color: colors.textDim,
    textDecorationLine: 'line-through',
    textDecorationColor: colors.border,
  },

  liveDotGap: {marginInlineStart: 6},

  planning: {
    paddingBlock: 8,
    paddingInline: 12,
    backgroundColor: colors.surface,
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.border,
    borderRadius: 3,
    fontSize: type.tUi,
    color: colors.textDim,
  },
  planningBar: {marginBlock: '6px 0', marginInline: 0},
  planningBarFill: {
    width: '60%',
    height: '100%',
    backgroundColor: colors.accent,
    animationName: kf.pulse,
    animationDuration: '1.2s',
    animationIterationCount: 'infinite',
  },
});
