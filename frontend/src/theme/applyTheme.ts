import * as stylex from '@stylexjs/stylex';

import {lightChannels, lightColors} from './themes.stylex';
import {colors, type} from './tokens.stylex';

export type Theme = 'light' | 'dark';

/**
 * `createTheme` compiles to a hashed class name, which the pre-paint script in
 * index.html cannot know — so that script only stamps `data-theme`, and this
 * puts the real theme class on <html> once the bundle runs. The two must stay
 * in step: `data-theme` still drives the pre-paint backdrop in base.css and the
 * `--pp-*` values __root.tsx reads for <meta name="theme-color">.
 *
 * Dark needs no class: it is the default baked into `defineVars`. Light is two
 * classes because it overrides two var groups — see themes.stylex.ts.
 */
const LIGHT_CLASSES = (stylex.props(lightChannels, lightColors).className ?? '')
  .split(' ')
  .filter(Boolean);

export function applyTheme(theme: Theme): void {
  const root = document.documentElement;
  root.setAttribute('data-theme', theme);
  for (const cls of LIGHT_CLASSES) {
    root.classList.toggle(cls, theme === 'light');
  }
}

/** The theme the pre-paint script already resolved from localStorage / OS. */
export function resolvedTheme(): Theme {
  return document.documentElement.getAttribute('data-theme') === 'light' ? 'light' : 'dark';
}

/**
 * `<body>` is React's parent, not its child, so no component can style it —
 * but its font and ambient field are design tokens, and duplicating those
 * as literals in base.css is what the pre-paint block already costs us once.
 * Instead the compiled class is put on the element by hand, at boot.
 */
const body = stylex.create({
  base: {
    fontFamily: type.body,
    backgroundImage: colors.appBg,
    backgroundAttachment: 'fixed',
    color: colors.text,
    // 100dvh, not 100vh: a phone's URL bar collapses and vh does not notice.
    height: '100dvh',
    overflow: 'hidden',
    fontSize: '1rem',
    WebkitFontSmoothing: 'antialiased',
    textRendering: 'optimizeLegibility',
    transition: 'color 0.25s ease, background-color 0.25s ease',
  },
});

export function applyBodyStyles(): void {
  const {className} = stylex.props(body.base);
  if (className) document.body.classList.add(...className.split(' ').filter(Boolean));
}
