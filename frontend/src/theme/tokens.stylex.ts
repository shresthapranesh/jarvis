/* ════════════════════════════════════════════════════════════════════
   Paper Terminal — ink on stock, for a console that is mostly reading.

   The previous theme ("Abyssal") built depth out of luminance: frosted
   panels, an ambient radial field, warm copper chrome. This one throws
   that model out. There is no glass, no gradient, no ambient wash and no
   brand hue — structure comes from hairline rules and whitespace, exactly
   the way a printed page gets it, and every surface is opaque.

   The one rule that governs the palette: **chrome is monochrome; colour
   is reserved for machine state.** The accent is ink (bone, inverted), so
   a filled button is a letterpress block and a link is underlined rather
   than tinted. The only hues on screen are the three agent signals and
   ok/danger/warn — which means a green dot is never competing with a
   copper button for the same glance.

   `channels` and `colors` are the two theme-varying groups, and the only
   ones `themes.stylex.ts` overrides — so every token that flips with the
   theme has to live in one of them. The split is not cosmetic: `colors`
   derives from `channels`, and a single group deriving from itself is a
   TS circularity error.

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
  tint: '234, 233, 228', // bone — every overlay is this ink/stock at some alpha
  shadow: '10, 10, 12',
  // The accent is the ink itself, faintly cooled. Chrome carries no hue, so a
  // filled control reads as a printed block and colour never competes with the
  // signal hues below for attention.
  accent: '232, 234, 240',

  // Semantic signal, reserved for live agent activity — muted to a printed
  // second-colour rather than a screen hue, so they sit on the stock instead
  // of glowing off it.
  signalLive: '95, 168, 118',
  signalTool: '108, 146, 200',
  signalInsight: '160, 137, 210',

  ok: '95, 168, 118',
  danger: '224, 106, 100',
  warn: '212, 164, 88',
});

export const colors = stylex.defineVars({
  // ── Neutrals (cold ink stock) ──
  bg: '#111113',
  surface: `rgba(${channels.tint}, 0.035)`,
  surface2: `rgba(${channels.tint}, 0.065)`,
  surface3: `rgba(${channels.tint}, 0.1)`,
  // Rules are the whole structural system now, so they carry roughly twice the
  // contrast the glass build needed — a 0.1 hairline disappears once there is
  // no luminance step behind it to imply the edge.
  border: `rgba(${channels.tint}, 0.15)`,
  borderStrong: `rgba(${channels.tint}, 0.28)`,
  text: '#e9e7e2',
  textDim: '#9b988f',
  textFaint: '#6a6862',

  // ── Interactive accent (ink, inverted) ──
  accent: `rgb(${channels.accent})`,
  accentStrong: '#ffffff',
  accentDim: `rgba(${channels.accent}, 0.12)`,
  accentContrast: '#111113', // text set on accent-filled controls

  // ── Semantic signal ──
  signalLive: `rgb(${channels.signalLive})`,
  signalTool: `rgb(${channels.signalTool})`,
  signalInsight: `rgb(${channels.signalInsight})`,
  signalLiveDim: `rgba(${channels.signalLive}, 0.15)`,
  signalToolDim: `rgba(${channels.signalTool}, 0.15)`,
  signalInsightDim: `rgba(${channels.signalInsight}, 0.15)`,

  // ── Status ──
  ok: `rgb(${channels.ok})`,
  danger: `rgb(${channels.danger})`,
  warn: `rgb(${channels.warn})`,
  // Text on a danger-filled control. In dark mode `danger` is a light salmon,
  // so white on it lands at 3.3:1 — under AA. Ink on it is 5.7:1, and it also
  // makes every filled button in dark mode dark-on-light, which is the rule
  // `accentContrast` already sets.
  dangerContrast: '#111113',
  userBg: `rgba(${channels.tint}, 0.06)`,
  errorBg: 'rgba(224, 106, 100, 0.1)',
  errorBorder: 'rgba(224, 106, 100, 0.34)',
  errorText: '#f0a5a0',
  warningBg: 'rgba(212, 164, 88, 0.1)',
  warningBorder: 'rgba(212, 164, 88, 0.32)',
  warningText: '#e3bd7f',
  webhookBg: 'rgba(160, 137, 210, 0.1)',
  webhookText: '#c3b0e8',

  // ── Panel stock ──
  // Opaque, not translucent: `glassBg` keeps its name because ~20 call sites
  // use it for "the panel over the page", but it is now a second sheet of
  // stock laid on the first. `layout.blur` is `none`, so the backdrop-filter
  // those sites still declare compiles to a no-op rather than needing a sweep.
  glassBg: '#17171a',
  glassBorder: `rgba(${channels.tint}, 0.13)`,
  // Flat. The ambient radial field was the single loudest thing in the old
  // theme; paper has no light source.
  appBg: 'linear-gradient(#111113, #111113)',
});

/**
 * The type scale. Eight steps, each with a job named below and nothing outside
 * the set — every `fontSize` in the app resolves to one of these.
 *
 * The steps are not arbitrary: they were derived by clustering the ~340
 * hand-written sizes this replaced, which had drifted to 42 distinct values
 * between 0.55rem and 1.4rem. Each cluster was a real intent stated in four
 * slightly different numbers; each step below is that intent, stated once.
 *
 * Two of them are worth explaining. `tPage` exists because every page title in
 * the app was written at 1.3–1.4rem while `tTitle` at 17px went unused — the
 * scale was missing a step, not the call sites. `tNano` exists for the workflow
 * canvas, where node badges and branch labels genuinely need to go below the
 * 11px eyebrow and were doing it at five different sizes.
 */
export const type = stylex.defineVars({
  // A text serif, not a grotesk: it is the one place the page admits it is a
  // document. Newsreader ships real weights — a display face faked to 600 by
  // the browser is exactly the tell this theme cannot afford.
  display: "'Newsreader', 'Iowan Old Style', Georgia, serif",
  body: "'Inter', system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif",
  mono: "'JetBrains Mono', ui-monospace, 'SF Mono', Menlo, Consolas, monospace",

  tDisplay: '2.125rem', // 34px — dispatch greeting; the only truly large type
  tPage: '1.3125rem', // 21px — page titles, stat values
  tTitle: '1.0625rem', // 17px — section + modal headings
  tBody: '0.9375rem', // 15px — prose, messages, dialog and panel copy
  tUi: '0.8125rem', // 13px — controls, nav, list rows
  tSmall: '0.75rem', // 12px — secondary meta
  tMicro: '0.6875rem', // 11px — eyebrows; always uppercase + tracked
  tNano: '0.625rem', // 10px — canvas badges and branch labels only
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

/**
 * Corners are a printing artefact in this theme, not a design element, so the
 * default is as close to none as makes no difference — `sm` is every chip,
 * control, list row and panel in the app.
 *
 * `md` and `lg` are the deliberate exception: the composer. It is the one
 * element you put your hands on rather than read, and it is allowed to be
 * soft. The pair is a nesting relationship, not two independent choices —
 * `lg` is the field, `md` is what sits inside it, and inner ≈ outer minus the
 * padding is what stops a rounded control inside a rounded field from looking
 * like a mistake.
 */
export const radii = stylex.defineVars({
  sm: '2px', // everything else
  md: '6px', // controls nested inside the composer
  lg: '12px', // the composer field
  xl: '16px', // unused; kept so the scale has headroom
});

export const layout = stylex.defineVars({
  leftW: '264px',
  leftCollapsedW: '52px',
  rightW: '312px',
  spineW: '208px',
  // The rail at rest: wide enough for the hairline and its 9px marks and
  // nothing else. Read by the rail itself *and* by the padding the thread and
  // composer reserve for it, which is the only reason it is a token.
  spineCollapsedW: '30px',
  // Kept as a token so the ~20 sites that still declare `backdropFilter` need
  // no edit: they now compile to a no-op. Panels are opaque stock instead.
  blur: 'none',
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
