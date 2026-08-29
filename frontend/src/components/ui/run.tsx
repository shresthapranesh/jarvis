/* ════════════════════════════════════════════════════════════════════
   Live-run ornaments and the shared parts of one exchange in a thread.
   ════════════════════════════════════════════════════════════════════ */
import * as stylex from '@stylexjs/stylex';

import {kf} from '../../theme/keyframes.stylex';
import {channels, colors, type} from '../../theme/tokens.stylex';

/**
 * Live-run ornaments: the blinking caret on a streaming reply, the bouncing
 * dots while a turn has no text yet, and the small green "in progress" dot.
 * Shared by the chat thread, the activity sidebar, the automation run panel
 * and live mode — all four render the same run, in different frames.
 */
export const stream = stylex.create({
  cursor: {
    display: 'inline-block',
    width: 2,
    height: '0.9em',
    backgroundColor: colors.accent,
    verticalAlign: 'text-bottom',
    marginInlineStart: 1,
    animationName: kf.blink,
    animationDuration: '0.9s',
    animationTimingFunction: 'step-end',
    animationIterationCount: 'infinite',
  },
  dots: {display: 'flex', gap: 4},
  dot: {
    display: 'inline-block',
    width: 7,
    height: 7,
    borderRadius: '50%',
    backgroundColor: colors.accent,
    animationName: kf.bounce,
    animationDuration: '1.2s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  // Staggered by hand rather than by :nth-child — the three dots are three
  // sibling elements, so the delay belongs on each.
  dot2: {animationDelay: '0.2s'},
  dot3: {animationDelay: '0.4s'},
  liveDot: {width: 6, height: 6, borderRadius: '50%', backgroundColor: colors.ok, flexShrink: 0},
  spinner: {
    animationName: kf.spin,
    animationDuration: '0.9s',
    animationTimingFunction: 'linear',
    animationIterationCount: 'infinite',
  },
});

/** Three dots that bounce in sequence — the "no text yet" state of a run. */
export function ThinkingDots() {
  return (
    <div {...stylex.props(stream.dots)}>
      <span {...stylex.props(stream.dot)} />
      <span {...stylex.props(stream.dot, stream.dot2)} />
      <span {...stylex.props(stream.dot, stream.dot3)} />
    </div>
  );
}

/** A spawned subagent's identity row — the role chip and its task. */
export const worker = stylex.create({
  dot: {flex: 'none', width: 7, height: 7, borderRadius: '50%'},
  dotRunning: {
    backgroundColor: colors.accent,
    animationName: kf.workerPulse,
    animationDuration: '1.2s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  dotDone: {backgroundColor: colors.accent, opacity: 0.55},
  dotError: {backgroundColor: '#d06a5c'},
  dotUnknown: {backgroundColor: colors.textDim, opacity: 0.5},
  role: {
    flex: 'none',
    fontSize: type.tMicro,
    fontWeight: 600,
    textTransform: 'uppercase',
    letterSpacing: '0.05em',
    color: colors.accent,
    backgroundColor: colors.accentDim,
    borderRadius: 2,
    paddingBlock: 1,
    paddingInline: 6,
  },
  task: {
    fontSize: type.tUi,
    color: colors.textDim,
    whiteSpace: 'nowrap',
    overflow: 'hidden',
    textOverflow: 'ellipsis',
  },
});

/**
 * A container for `marked` output.
 *
 * `data-md` on the element is what the descendant rules in base.css hook —
 * those style <p>/<pre>/<table> nodes that only exist as an HTML string at
 * runtime. This half carries the container's own typography plus the token
 * hand-off: a stylesheet cannot name StyleX's hashed variables, so the tokens
 * those rules need are re-published here under stable `--md-*` names.
 */
export const prose = stylex.create({
  /**
   * The `--md-*` publication on its own, for a container that renders
   * `marked` output but sets its own type scale. `[data-md]` in base.css
   * reads these; the element still needs the `data-md` attribute.
   */
  vars: {
    '--md-surface': colors.surface,
    '--md-surface2': colors.surface2,
    '--md-border': colors.border,
    '--md-text-dim': colors.textDim,
    '--md-accent': colors.accent,
    '--md-inset': `rgba(${channels.tint}, 0.04)`,
  },
  base: {
    paddingBlock: 3,
    paddingInline: 2,
    lineHeight: 1.68,
    fontSize: type.tBody,
    letterSpacing: '-0.01em',
    wordBreak: 'break-word',
    '--md-surface': colors.surface,
    '--md-surface2': colors.surface2,
    '--md-border': colors.border,
    '--md-text-dim': colors.textDim,
    '--md-accent': colors.accent,
    '--md-inset': `rgba(${channels.tint}, 0.04)`,
  },
  /** Historical rows the removed safety gates persisted as `blocked`. */
  blocked: {
    borderWidth: 1,
    borderStyle: 'dashed',
    borderColor: colors.warningBorder,
    backgroundColor: colors.warningBg,
    borderRadius: 3,
    paddingBlock: 12,
    paddingInline: 14,
    opacity: 0.85,
  },
});

/**
 * One exchange in a thread — the 740px measure every message, plan card and
 * error shares. Lives here because MessageThread, MessageBubble and the
 * conversation route all render one.
 */
export const turn = stylex.create({
  base: {
    display: 'flex',
    flexDirection: 'column',
    gap: 7,
    width: '100%',
    maxWidth: 740,
    marginBlock: 0,
    marginInline: 'auto',
    animationName: kf.msgEnter,
    animationDuration: '0.28s',
    animationTimingFunction: 'cubic-bezier(0.32, 0.72, 0, 1)',
    animationFillMode: 'both',
    // Resting turns show their action row at 0.4 and bring it up on hover; a
    // child cannot see the turn's :hover, so the turn publishes it.
    '--turn-actions-opacity': {default: '0.4', ':hover': '1'},
  },
});

/** A failed turn or action, rendered inline where the result would have gone. */
export const errorBubble = stylex.create({
  base: {
    backgroundColor: colors.errorBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.errorBorder,
    borderRadius: 3,
    paddingBlock: 10,
    paddingInline: 14,
    color: colors.errorText,
    fontSize: type.tBody,
  },
});

/**
 * The dimmed, blurred sheet behind any modal, and the raised panel on it.
 * Shared by ConfirmDialog, FormModal and the model-sync dialog.
 */
