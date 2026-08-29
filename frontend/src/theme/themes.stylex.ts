import * as stylex from '@stylexjs/stylex';

import {channels, colors} from './tokens.stylex';

/* ── Light mode — ink on bone stock ────────────────────────────────────
   Dark is the default (it lives in `defineVars`), so light is the only theme
   here. It comes in two halves because `createTheme` overrides one var group
   at a time, and `colors` derives from `channels`; `applyTheme.ts` puts both
   classes on <html> together.

   The two modes are the *same* design, not two designs: flat opaque stock,
   hairline rules, monochrome chrome, colour only on machine state. What flips
   is which of ink/stock is the ground — so `tint` and `accent` swap roles and
   almost everything derived from them follows without restating.

   The four literals in base.css's pre-paint block mirror `bg`/`text` from
   here and from tokens.stylex.ts — changing either colour means changing both.
   ─────────────────────────────────────────────────────────────────────── */

/**
 * Swapping these re-tints every overlay derived from them in `lightColors`
 * below, which is why most of the dark palette's `rgba(...)` tokens need no
 * restating at all. `tint` and `accent` are both ink here and both bone in
 * dark: the overlay hue and the accent are the same substance in this theme.
 */
export const lightChannels = stylex.createTheme(channels, {
  tint: '22, 22, 26',
  shadow: '22, 22, 26',
  accent: '26, 27, 33',
  // The signal inks darken on stock — the dark-mode values fail contrast on
  // #f4f2ed, and a printed second colour is darker than its screen twin anyway.
  signalLive: '30, 116, 71',
  signalTool: '42, 91, 158',
  signalInsight: '95, 66, 175',
  ok: '30, 116, 71',
  danger: '186, 46, 40',
  warn: '146, 98, 20',
});

/**
 * Only two kinds of token appear here: literal colours, and derived overlays
 * whose *alpha* differs from dark. The derived tokens whose formula is
 * unchanged (`surface*`, `accent`, `signal*Dim`, `ok`, `danger`, `warn`,
 * `userBg`) follow the channel swap above on their own, and are absent on
 * purpose.
 */
export const lightColors = stylex.createTheme(colors, {
  bg: '#f4f2ed',
  // Ink rules on stock read heavier than bone rules on ink at equal alpha, so
  // both steps come down rather than being inherited.
  border: `rgba(${channels.tint}, 0.13)`,
  borderStrong: `rgba(${channels.tint}, 0.24)`,
  text: '#16161a',
  textDim: '#5e5c57',
  textFaint: '#8d8a83',

  accentStrong: '#000000',
  accentDim: `rgba(${channels.accent}, 0.09)`,
  accentContrast: '#f7f6f2',
  dangerContrast: '#f7f6f2', // light-mode `danger` is a deep red: paper on it, 5.5:1

  errorText: '#a11f1a',
  errorBg: 'rgba(186, 46, 40, 0.07)',
  errorBorder: 'rgba(186, 46, 40, 0.26)',
  warningText: '#7c5310',
  warningBg: 'rgba(146, 98, 20, 0.08)',
  warningBorder: 'rgba(146, 98, 20, 0.28)',
  webhookText: '#553da0',
  webhookBg: 'rgba(95, 66, 175, 0.07)',

  // A second sheet of stock, one step brighter than the page — opaque, so it
  // cannot pick up the tint swap the way a translucent panel would.
  glassBg: '#fbfaf7',
  glassBorder: `rgba(${channels.tint}, 0.11)`,
  appBg: 'linear-gradient(#f4f2ed, #f4f2ed)',
});
