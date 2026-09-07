import {useEffect, useState} from 'react';

import {fetchBrowserAvailable} from '../relay/BrowserAvailableQuery';

/**
 * Whether there is a browser to watch, asked of the server rather than inferred.
 *
 * The predecessor of this hook was a value derived from `browser_step` events,
 * which made the button that opens the browser panel disappear whenever the run
 * that produced those events went away — when the read finished, and again when
 * a new message reset the stream state. The browser is a singleton that outlives
 * all of that, so the honest question is "is one running", and only the server
 * can answer it.
 *
 * Fetched on mount and re-checked on `trigger`, which callers bump when a browse
 * is announced — that is the one moment a browser can appear mid-session. No
 * polling: a browser that starts for any other reason is picked up on the next
 * page load, and this must not add a timer to every conversation.
 */
export function useBrowserAvailable(trigger: unknown): boolean {
  const [available, setAvailable] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetchBrowserAvailable()
      .then((ok) => {
        if (!cancelled) setAvailable(ok);
      })
      // Failing soft is the point: this decides whether to show a button, and
      // a failed probe must never blank the conversation it sits in.
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [trigger]);

  return available;
}
