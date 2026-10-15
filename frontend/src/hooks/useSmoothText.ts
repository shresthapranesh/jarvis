import {useEffect, useRef, useState} from 'react';

/** A backlog is spent over roughly this long, so the reveal never trails far. */
const DRAIN_MS = 180;
/**
 * Once the stream has ended nothing more is coming to pace against, and the
 * stored message is about to replace this text whole — so finish quickly
 * rather than have the tail pop in when it does.
 */
const DRAIN_DONE_MS = 50;
/** The floor, so the last few words of a burst don't crawl in. */
const MIN_CPS = 90;
/** How far past the cut the reveal may run to finish the word it is in. */
const WORD_REACH = 12;

/**
 * Streamed text revealed at a steady pace instead of in the bursts it arrives
 * in. Providers hand tokens over in batches — Gemini sends ~20 words every
 * ~100ms — so painting each batch as it lands reads as the reply jumping a
 * line at a time. This trails the real text by a fraction of a second and
 * spends the backlog every frame, faster the further behind it is.
 *
 * Text already present when the hook mounts (a run opened mid-stream) shows at
 * once; only what arrives afterwards is paced. The reveal ends on a word
 * boundary where one is near, so a word never appears half-typed.
 */
export function useSmoothText(target: string, ended = false): string {
  const [shown, setShown] = useState(() => target.length);
  const shownRef = useRef(shown);
  const targetRef = useRef(target);
  targetRef.current = target;
  const drainRef = useRef(DRAIN_MS);
  drainRef.current = ended ? DRAIN_DONE_MS : DRAIN_MS;

  useEffect(() => {
    // The target was replaced, not extended (a new run reset it): start over.
    if (shownRef.current > target.length) {
      shownRef.current = target.length;
      setShown(target.length);
    }
    if (shownRef.current >= target.length) return;

    let raf = 0;
    let last = performance.now();
    const step = (now: number) => {
      const text = targetRef.current;
      const dt = now - last;
      last = now;
      const backlog = text.length - shownRef.current;
      if (backlog <= 0) return;

      let next =
        shownRef.current + Math.max(1, (MIN_CPS * dt) / 1000, (backlog * dt) / drainRef.current);
      next = Math.min(text.length, Math.ceil(next));
      const toSpace = text.slice(next, next + WORD_REACH).search(/\s/);
      if (toSpace > 0) next += toSpace;

      shownRef.current = next;
      setShown(next);
      if (next < text.length) raf = requestAnimationFrame(step);
    };
    raf = requestAnimationFrame(step);
    return () => cancelAnimationFrame(raf);
  }, [target, ended]);

  return target.slice(0, Math.min(shown, target.length));
}
