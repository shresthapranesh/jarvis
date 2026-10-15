import * as stylex from '@stylexjs/stylex';
import {Link} from '@tanstack/react-router';

import {decodeGlobalId} from '../relay/globalId';
import {kf} from '../theme/keyframes.stylex';
import {colors, layout, space, type} from '../theme/tokens.stylex';
import {FolderIcon} from './icons';

export type RunState = 'running' | 'waiting' | null;

interface Props {
  title: string | null;
  project: {id: string; name: string} | null;
  createdAt: string;
  runState: RunState;
  /** Mirrors the footer: how much of the right edge the run spine takes. */
  spine: 'none' | 'open' | 'collapsed';
}

/** "Aug 14", or "Aug 14, 2025" once it is not this year. */
function startedOn(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString(undefined, {
    month: 'short',
    day: 'numeric',
    ...(sameYear ? {} : {year: 'numeric'}),
  });
}

/**
 * The conversation's name and where it lives, held above the thread so the
 * reader keeps their bearings once the first prompt has scrolled away.
 *
 * Its content sits in the thread's own 740px column, so the title lines up
 * with the prose under it rather than with the page edge. The model is left
 * out on purpose: the composer's picker shows it, and is always on screen.
 */
export function ConversationHeader({title, project, createdAt, runState, spine}: Props) {
  const started = startedOn(createdAt);
  return (
    <header
      {...stylex.props(
        styles.bar,
        spine === 'open' && styles.withSpine,
        spine === 'collapsed' && styles.withSpineCollapsed,
      )}
    >
      <div {...stylex.props(styles.column)}>
        {project && (
          <>
            <Link
              to="/projects/$id"
              params={{id: decodeGlobalId(project.id)}}
              {...stylex.props(styles.project)}
              title="Open project"
            >
              <FolderIcon size={12} />
              <span {...stylex.props(styles.projectName)}>{project.name}</span>
            </Link>
            <span {...stylex.props(styles.sep)} aria-hidden="true">
              /
            </span>
          </>
        )}
        <h1 {...stylex.props(styles.title, !title && styles.titleEmpty)} title={title ?? undefined}>
          {title ?? 'New conversation'}
        </h1>
        <span {...stylex.props(styles.meta)}>
          {runState === 'running' ? (
            <span {...stylex.props(styles.state, styles.stateLive)}>
              <span {...stylex.props(styles.dot, styles.dotLive)} />
              Running
            </span>
          ) : runState === 'waiting' ? (
            <span {...stylex.props(styles.state, styles.stateWaiting)}>
              <span {...stylex.props(styles.dot, styles.dotWaiting)} />
              Waiting for you
            </span>
          ) : (
            started && (
              <time dateTime={createdAt} title={new Date(createdAt).toLocaleString()}>
                {started}
              </time>
            )
          )}
        </span>
      </div>
    </header>
  );
}

const styles = stylex.create({
  bar: {
    flexShrink: 0,
    paddingBlock: 10,
    // The thread scroller's own gutter, so the column below lines up exactly.
    paddingInline: {default: 20, '@media (max-width: 768px)': 14},
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    backgroundColor: colors.bg,
  },
  /**
   * The run spine is fixed to the right edge, full height, so the bar reserves
   * its width the same way the thread and footer do — breakpoints included.
   */
  withSpine: {
    paddingInlineEnd: {
      default: `calc(${layout.spineW} + ${space.s5})`,
      '@media (max-width: 1100px)': space.s5,
      '@media (max-width: 768px)': 14,
    },
  },
  withSpineCollapsed: {
    paddingInlineEnd: {
      default: `calc(${layout.spineCollapsedW} + ${space.s5})`,
      '@media (max-width: 1100px)': space.s5,
      '@media (max-width: 768px)': 14,
    },
  },
  column: {
    display: 'flex',
    alignItems: 'center',
    gap: space.s2,
    maxWidth: 740,
    marginInline: 'auto',
    minWidth: 0,
    minHeight: 24,
  },
  project: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 6,
    flexShrink: 1,
    minWidth: 0,
    maxWidth: '40%',
    fontSize: type.tUi,
    color: {default: colors.textDim, ':hover': colors.text},
    textDecoration: 'none',
    transition: 'color 0.15s ease',
  },
  projectName: {overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'},
  sep: {color: colors.textFaint, fontSize: type.tUi, flexShrink: 0},
  title: {
    flex: 1,
    minWidth: 0,
    margin: 0,
    fontSize: type.tUi,
    fontWeight: 600,
    lineHeight: 1.4,
    color: colors.text,
    overflow: 'hidden',
    textOverflow: 'ellipsis',
    whiteSpace: 'nowrap',
  },
  titleEmpty: {color: colors.textDim, fontWeight: 500},
  meta: {
    flexShrink: 0,
    fontSize: type.tSmall,
    color: colors.textFaint,
    fontVariantNumeric: 'tabular-nums',
  },
  state: {display: 'inline-flex', alignItems: 'center', gap: 6},
  stateLive: {color: colors.signalLive},
  stateWaiting: {color: colors.warn},
  dot: {width: 6, height: 6, borderRadius: '50%', flexShrink: 0},
  dotLive: {
    backgroundColor: colors.signalLive,
    animationName: kf.pulse,
    animationDuration: '2.4s',
    animationIterationCount: 'infinite',
  },
  dotWaiting: {backgroundColor: colors.warn},
});
