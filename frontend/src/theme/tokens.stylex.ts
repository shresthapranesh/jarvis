/* ════════════════════════════════════════════════════════════════════
   Abyssal — a deep-sea, instrument-grade theme for a multi-agent console.
   One petrol hue carries the whole field; depth comes from luminance, not
   from mixing hues. Panels are frosted petrol glass. Two modes share one
   identity: neutrals + interactive accent flip per theme; the semantic
   *signal* hues (one per agent role / event kind) stay constant, retuned
   only for contrast. Every light/dark overlay in this file is driven off
   `channels` so a single channel swap re-tints the UI.

   `channels` and `colors` are the two theme-varying groups, and the only
   ones `themes.stylex.ts` overrides — so every token that flips with the
   theme has to live in one of them, including the glass and the ambient
   field. The split is not cosmetic: `colors` derives from `channels`, and
   a single group deriving from itself is a TS circularity error.

   Everything below them (type, space, layout, radii) is theme-invariant
   and gets its own group so it never has to be restated in a theme.
   ════════════════════════════════════════════════════════════════════ */
import * as stylex from '@stylexjs/stylex';

/**
 * The raw hues, as comma-separated channel triples so they can be composed
 * into `rgba()` at any alpha. Overriding these in a theme re-tints every
 * derived token in `colors` below without the theme restating any of them.
 */
export const channels = stylex.defineVars({
  tint: '247, 235, 222', // warm bone — every overlay carries the ember cast
  shadow: '12, 7, 4',
  accent: '224, 138, 69', // copper — chrome, links, focus

  // Semantic signal, reserved for live agent activity. `tool` is cyan, not the
  // usual amber: the accent is copper here, and two warm hues a step apart stop
  // reading as different signals on a 9px mark.
  signalLive: '110, 222, 143',
  signalTool: '92, 200, 216',
  signalInsight: '180, 148, 248',

  ok: '110, 222, 143',
  danger: '248, 113, 113',
  warn: '240, 180, 94',
});

export const colors = stylex.defineVars({
  // ── Neutrals (warm graphite) ──
  bg: '#141110',
  surface: `rgba(${channels.tint}, 0.045)`,
  surface2: `rgba(${channels.tint}, 0.075)`,
  surface3: `rgba(${channels.tint}, 0.11)`,
  border: `rgba(${channels.tint}, 0.1)`,
  borderStrong: `rgba(${channels.tint}, 0.17)`,
  text: '#ece6df',
  textDim: '#9a8e84',
  textFaint: '#6b6058',

  // ── Interactive accent ──
  accent: `rgb(${channels.accent})`,
  accentStrong: '#f0a668',
  accentDim: `rgba(${channels.accent}, 0.14)`,
  accentContrast: '#1e1006', // text set on accent-filled controls

  // ── Semantic signal ──
  signalLive: `rgb(${channels.signalLive})`,
  signalTool: `rgb(${channels.signalTool})`,
  signalInsight: `rgb(${channels.signalInsight})`,
  signalLiveDim: `rgba(${channels.signalLive}, 0.13)`,
  signalToolDim: `rgba(${channels.signalTool}, 0.13)`,
  signalInsightDim: `rgba(${channels.signalInsight}, 0.13)`,

  // ── Status ──
  ok: `rgb(${channels.ok})`,
  danger: `rgb(${channels.danger})`,
  warn: `rgb(${channels.warn})`,
  userBg: `rgba(${channels.accent}, 0.15)`,
  errorBg: 'rgba(58, 20, 16, 0.55)',
  errorBorder: 'rgba(150, 55, 45, 0.5)',
  errorText: '#fca5a5',
  warningBg: 'rgba(56, 36, 12, 0.5)',
  warningBorder: 'rgba(180, 120, 40, 0.45)',
  warningText: '#f3c07a',
  webhookBg: 'rgba(46, 30, 62, 0.5)',
  webhookText: '#cbb6fd',

  // ── Glass + ambient field (one hue, depth by luminance) ──
  glassBg: 'rgba(26, 21, 18, 0.58)',
  glassBorder: `rgba(${channels.tint}, 0.09)`,
  appBg:
    'radial-gradient(ellipse 120% 85% at 50% -18%, #2a1d14 0%, rgba(42, 29, 20, 0) 62%), linear-gradient(180deg, #1a1512 0%, #141110 52%, #0f0c0a 100%)',
});

/**
 * Six steps, each with an intended job; nothing outside this set. The shell
 * used to render almost everything at ~13px, which is why it read as flat.
 */
export const type = stylex.defineVars({
  display: "'Space Grotesk', system-ui, sans-serif",
  body: "'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  mono: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace",

  tDisplay: '1.75rem', // dispatch greeting — the only truly large type
  tTitle: '1.0625rem', // page titles
  tBody: '0.9375rem', // prose, messages
  tUi: '0.8125rem', // controls, nav, list rows
  tSmall: '0.75rem', // secondary meta
  tMicro: '0.6875rem', // eyebrows — always uppercase + tracked
  trackMicro: '0.13em',
});

export const space = stylex.defineVars({
  s1: '4px',
  s2: '8px',
  s3: '12px',
  s4: '16px',
  s5: '24px',
  s6: '36px',
  s7: '56px',
});

export const radii = stylex.defineVars({
  sm: '6px',
  md: '10px',
  lg: '14px',
  xl: '18px',
});

export const layout = stylex.defineVars({
  leftW: '264px',
  leftCollapsedW: '52px',
  rightW: '312px',
  spineW: '208px',
  blur: 'blur(14px)',
});

/**
 * Values that are CSS syntax rather than design tokens.
 *
 * `defineConsts` is not optional here, and a plain exported string is a silent
 * trap: every export of a `.stylex.ts` file is treated as a StyleX constant, so
 * `export const KB_INSET = 'var(--kb-inset, 0px)'` compiles to a *hashed
 * identifier* in the output — `calc(100dvh - x1yosntj)` — which is invalid CSS,
 * and the browser drops the whole declaration with no error anywhere.
 * `defineConsts` inlines the literal, which is what these need.
 *
 * `kbInset` is the height of the layout viewport hidden by the virtual
 * keyboard. It stays a hand-written custom property because `__root.tsx` writes
 * it imperatively from a visualViewport listener, and a hashed name would break
 * that call — it is a runtime measurement, not a design decision.
 */
export const css = stylex.defineConsts({
  kbInset: 'var(--kb-inset, 0px)',
  safeTop: 'env(safe-area-inset-top, 0px)',
  safeRight: 'env(safe-area-inset-right, 0px)',
  safeBottom: 'env(safe-area-inset-bottom, 0px)',
  safeLeft: 'env(safe-area-inset-left, 0px)',
});

/**
 * Breakpoints as consts rather than vars: they are inlined at build time, so
 * they can be used as object keys in a conditional style value.
 */
export const bp = stylex.defineConsts({
  wide: '@media (max-width: 1100px)',
  tablet: '@media (max-width: 900px)',
  compact: '@media (max-width: 860px)',
  mobile: '@media (max-width: 768px)',
  narrow: '@media (max-width: 600px)',
  coarse: '@media (hover: none)',
  motionOk: '@media (prefers-reduced-motion: no-preference)',
});
