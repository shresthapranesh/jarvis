/** Every effort level, lowest first — the server's `llm::EFFORTS`. A model's
 *  levels are picked from these; each provider gets them as its own setting. */
export const EFFORT_LEVELS = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max'] as const;

const LABELS: Record<string, string> = {
  none: 'Off',
  minimal: 'Minimal',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
  xhigh: 'Extra high',
  max: 'Max',
};

export function effortLabel(level: string): string {
  return LABELS[level] ?? level;
}

/** What a chat runs at with no pick and no default set (`agent::effort`). */
const CHAT_FALLBACK = 'medium';

const rank = (l: string) => EFFORT_LEVELS.indexOf(l as (typeof EFFORT_LEVELS)[number]);

/**
 * The level a turn will actually ask `model` for: the pick, else the default,
 * else a chat's own — then the nearest the model takes, the lower of two as
 * near, and `none` only when asked for. The server does the same
 * (`llm::nearest_effort`). Null when the model takes none.
 */
export function effectiveEffort(
  chosen: string | null | undefined,
  defaultEffort: string | null | undefined,
  modelLevels: readonly string[],
): string | null {
  const want = chosen ?? defaultEffort ?? CHAT_FALLBACK;
  if (modelLevels.includes(want)) return want;
  let best: string | null = null;
  for (const l of modelLevels) {
    if (l === 'none') continue;
    const d = Math.abs(rank(l) - rank(want));
    // Lowest first, so a tie keeps the lower.
    if (best === null || d < Math.abs(rank(best) - rank(want))) best = l;
  }
  return best;
}
