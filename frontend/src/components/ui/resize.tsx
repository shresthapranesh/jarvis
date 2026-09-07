import * as stylex from '@stylexjs/stylex';
import {useCallback, useEffect, useRef, useState} from 'react';

import {colors} from '../../theme/tokens.stylex';

/* ── A draggable edge for the fixed side panels ────────────────────────
   The panels here are `position: fixed` against the inline-end edge, so
   "resizing" is only ever setting a width — no flex parent to negotiate
   with and no layout to thrash. That keeps this a hook plus a handle
   rather than a split-pane component.

   Width is per-panel and persisted, because a panel you resized and then
   closed should come back the size you left it. */

const MIN_WIDTH = 320;
/** Never let a panel swallow the thread it is annotating. */
const MAX_FRACTION = 0.9;

function clamp(width: number): number {
  return Math.max(MIN_WIDTH, Math.min(width, window.innerWidth * MAX_FRACTION));
}

export function useResizableWidth(storageKey: string, initial: number) {
  const [width, setWidth] = useState(() => {
    try {
      const stored = window.localStorage.getItem(storageKey);
      if (stored) return clamp(Number(stored) || initial);
    } catch {
      // Private windows and blocked site data both throw on read.
    }
    return initial;
  });

  useEffect(() => {
    try {
      window.localStorage.setItem(storageKey, String(width));
    } catch {
      // A width we cannot persist is still a width that works this session.
    }
  }, [storageKey, width]);

  // A viewport that shrinks below the stored width would leave the panel
  // wider than the screen with no way back except dragging it.
  useEffect(() => {
    const onResize = () => setWidth((w) => clamp(w));
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  return [width, setWidth] as const;
}

export function ResizeHandle({
  width,
  onResize,
  label,
}: {
  width: number;
  onResize: (width: number) => void;
  label: string;
}) {
  const dragging = useRef(false);

  const onPointerDown = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    dragging.current = true;
    // Capture so a fast drag that outruns the 6px handle keeps sending moves
    // to it rather than to whatever is under the cursor.
    e.currentTarget.setPointerCapture(e.pointerId);
  }, []);

  const onPointerMove = useCallback(
    (e: React.PointerEvent<HTMLDivElement>) => {
      if (!dragging.current) return;
      // Panels are pinned to the inline-end edge, so width is the distance
      // from the pointer to the right of the viewport.
      onResize(clamp(window.innerWidth - e.clientX));
    },
    [onResize],
  );

  const onPointerUp = useCallback((e: React.PointerEvent<HTMLDivElement>) => {
    dragging.current = false;
    e.currentTarget.releasePointerCapture(e.pointerId);
  }, []);

  // Keyboard resizing is the only path for anyone not using a pointer, and a
  // separator with no keyboard affordance is a control they simply cannot use.
  const onKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      const step = e.shiftKey ? 100 : 20;
      // Left grows the panel: it is pinned to the inline-end edge, so its
      // draggable border moves toward the start as it gets wider.
      if (e.key === 'ArrowLeft') {
        e.preventDefault();
        onResize(clamp(width + step));
      } else if (e.key === 'ArrowRight') {
        e.preventDefault();
        onResize(clamp(width - step));
      }
    },
    [onResize, width],
  );

  return (
    <div
      {...stylex.props(handle.root)}
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width}
      aria-valuemin={MIN_WIDTH}
      aria-valuemax={Math.round(window.innerWidth * MAX_FRACTION)}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      onKeyDown={onKeyDown}
    />
  );
}

const handle = stylex.create({
  root: {
    position: 'absolute',
    insetBlock: 0,
    insetInlineStart: 0,
    // Wider than it looks: a 1px target is a 1px target.
    width: 7,
    marginInlineStart: -3,
    cursor: 'col-resize',
    touchAction: 'none',
    zIndex: 2,
    backgroundColor: {
      default: 'transparent',
      ':hover': colors.accent,
      ':focus-visible': colors.accent,
    },
    opacity: {default: 1, ':hover': 0.5, ':focus-visible': 0.7},
    outline: 'none',
    transitionProperty: 'background-color',
    transitionDuration: '120ms',
  },
});
