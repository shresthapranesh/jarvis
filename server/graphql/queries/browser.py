"""Is there a browser worth watching right now?

The browser panel streams an app-level singleton that outlives every run, so
the affordance that opens it must not be derived from one run's event stream.
It was, twice: the "Browsing" chip is built from `browser_step` events, and
`useTaskEvents` wipes its state on every new task id — so sending a second
message removed the only way back into the panel.

This answers the question directly instead. A probe of the CDP endpoint is a
sub-millisecond local request, and its answer is true exactly when there is
something to look at — after a reload, in a conversation that never browsed,
whatever the run state happens to be.
"""

from __future__ import annotations

import asyncio
import logging

import strawberry

logger = logging.getLogger(__name__)


@strawberry.type
class BrowserQuery:
    @strawberry.field
    async def browser_available(self) -> bool:
        """Whether a CDP browser is reachable at the configured endpoint.

        Never raises and never launches one: this runs on a page load, and a
        UI probe must not be what starts a browser window. `_endpoint_live` is
        blocking httpx, so it goes to a thread rather than parking the loop
        that is also serving every live subscription.
        """
        try:
            from tools.browser import _endpoint_live, cdp_url

            return await asyncio.to_thread(_endpoint_live, cdp_url())
        except Exception:
            logger.debug("browser availability probe failed", exc_info=True)
            return False
