import * as stylex from '@stylexjs/stylex';

import {channels, colors} from './tokens.stylex';

/* ── Light mode — graphite ink overlays on a warm paper field ──────────
   Dark is the default (it lives in `defineVars`), so light is the only theme
   here. It comes in two halves because `createTheme` overrides one var group
   at a time, and `colors` derives from `channels`; `applyTheme.ts` puts both
   classes on <html> together.

   The four literals in base.css's pre-paint block mirror `bg`/`text` from
   here and from tokens.stylex.ts — changing either colour means changing both.
   ─────────────────────────────────────────────────────────────────────── */

/**
 * Swapping these re-tints every overlay derived from them in `lightColors`
 * below, which is why most of the dark palette's `rgba(...)` tokens need no
 * restating at all.
 */
export const lightChannels = stylex.createTheme(channels, {
  tint: '32, 22, 16',
  shadow: '28, 18, 10',
  // Copper darkens on paper — the dark-mode value fails contrast on #f6f3ee.
  accent: '180, 98, 42',
  signalLive: '13, 132, 84',
  signalTool: '17, 122, 140',
  signalInsight: '109, 68, 216',
  ok: '13, 132, 84',
  danger: '202, 42, 42',
  warn: '158, 104, 22',
});

/**
 * Only two kinds of token appear here: literal colours, and derived overlays
 * whose *alpha* differs from dark. The derived tokens whose formula is
 * unchanged (`border`, `accent`, `signal*`, `ok`, `danger`, `warn`) follow the
 * channel swap above on their own, and are absent on purpose.
 */
export const lightColors = stylex.createTheme(colors, {
  bg: '#f6f3ee',
  surface: `rgba(${channels.tint}, 0.035)`,
  surface2: `rgba(${channels.tint}, 0.06)`,
  surface3: `rgba(${channels.tint}, 0.1)`,
  borderStrong: `rgba(${channels.tint}, 0.18)`,
  text: '#1a1512',
  textDim: '#66594f',
  textFaint: '#948779',

  accentStrong: '#944d1f',
  accentDim: `rgba(${channels.accent}, 0.12)`,
  accentContrast: '#fdf7f1',

  signalLiveDim: `rgba(${channels.signalLive}, 0.12)`,
  signalToolDim: `rgba(${channels.signalTool}, 0.12)`,
  signalInsightDim: `rgba(${channels.signalInsight}, 0.12)`,

  userBg: `rgba(${channels.accent}, 0.12)`,
  errorBg: 'rgba(202, 42, 42, 0.08)',
  errorBorder: 'rgba(202, 42, 42, 0.28)',
  errorText: '#a81f1f',
  warningBg: 'rgba(158, 104, 22, 0.1)',
  warningBorder: 'rgba(158, 104, 22, 0.3)',
  warningText: '#855312',
  webhookBg: 'rgba(109, 68, 216, 0.08)',
  webhookText: '#5b33c0',

  // near-white glass (not pure #fff, so the channel swap can't touch it)
  glassBg: 'rgba(253, 251, 248, 0.78)',
  glassBorder: `rgba(${channels.tint}, 0.08)`,
  appBg:
    'radial-gradient(ellipse 120% 85% at 50% -18%, #fdfbf7 0%, rgba(253, 251, 247, 0) 62%), linear-gradient(180deg, #faf7f2 0%, #f6f3ee 52%, #f0ece5 100%)',
});
