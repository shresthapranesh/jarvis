import * as stylex from '@stylexjs/stylex';
import type {ReactNode} from 'react';

import {colors, type} from '../../theme/tokens.stylex';
import {page} from './page';

interface Props {
  title: ReactNode;
  /** One plain sentence on what the page is for. */
  subtitle?: ReactNode;
  /** The page's own buttons, top right; they take their own row on a phone. */
  actions?: ReactNode;
  /**
   * How the page works under the hood, for whoever wants it. Folded away under
   * "How this works" so the subtitle can stay one sentence.
   */
  details?: ReactNode;
  /** Anything that belongs under the subtitle, such as a row of counts. */
  children?: ReactNode;
}

/**
 * The title block every top-level page opens with, so the title sits at the
 * same size and in the same place on all of them. It belongs inside
 * `page.scroll` (a page that scrolls as one document) or `page.bar` (a page
 * that pins its header above a scrolling body).
 */
export function PageHeader({title, subtitle, actions, details, children}: Props) {
  return (
    <header {...stylex.props(page.header)}>
      <div {...stylex.props(page.headerMain)}>
        <h1 {...stylex.props(page.title)}>{title}</h1>
        {subtitle && <p {...stylex.props(page.subtitle)}>{subtitle}</p>}
        {details && (
          <details {...stylex.props(styles.details)}>
            <summary {...stylex.props(styles.summary)}>How this works</summary>
            <div {...stylex.props(styles.detailsBody)}>{details}</div>
          </details>
        )}
        {children}
      </div>
      {actions && <div {...stylex.props(page.headerActions)}>{actions}</div>}
    </header>
  );
}

const styles = stylex.create({
  details: {marginBlockStart: 6, maxWidth: 640},
  summary: {
    display: 'inline-block',
    cursor: 'pointer',
    fontSize: type.tSmall,
    color: {default: colors.textFaint, ':hover': colors.textDim},
    userSelect: 'none',
  },
  detailsBody: {
    marginBlockStart: 6,
    fontSize: type.tUi,
    lineHeight: 1.55,
    color: colors.textDim,
  },
});
