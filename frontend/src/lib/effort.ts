/** Every effort level, lowest first — the server's `EFFORTS`. */
export const EFFORT_LEVELS = ['low', 'medium', 'high', 'xhigh', 'max'] as const;

const LABELS: Record<string, string> = {
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

/**
 * The level a turn will actually ask `model` for: the pick, else the default,
 * else a chat's own — stepped down to the highest level the model takes, as
 * the server does (`anthropic::effort_for`). Null when the model takes none.
 */
export function effectiveEffort(
  chosen: string | null | undefined,
  defaultEffort: string | null | undefined,
  modelLevels: readonly string[],
): string | null {
  if (modelLevels.length === 0) return null;
  const want = chosen ?? defaultEffort ?? CHAT_FALLBACK;
  const rank = (l: string) => EFFORT_LEVELS.indexOf(l as (typeof EFFORT_LEVELS)[number]);
  const below = modelLevels.filter((l) => rank(l) <= rank(want));
  return below.length ? below[below.length - 1] : modelLevels[0];
}
