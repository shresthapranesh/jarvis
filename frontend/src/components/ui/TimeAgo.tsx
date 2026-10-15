import {formatRelativeTime} from '../../lib/api';
import {fullDateTime, relativeTime} from '../../lib/format';

/**
 * A timestamp as people read one: relative while it is recent ("4m ago"), the
 * date once it is a week old, and the exact moment on hover. Every list, card
 * and header shows time through this, so they all agree.
 */
export function TimeAgo({
  iso,
  compact,
}: {
  iso: string;
  /** "4m" for dense rails. */ compact?: boolean;
}) {
  return (
    <time dateTime={iso} title={fullDateTime(iso)}>
      {compact ? relativeTime(iso) : formatRelativeTime(iso)}
    </time>
  );
}
