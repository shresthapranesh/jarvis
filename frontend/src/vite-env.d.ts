/// <reference types="vite/client" />

// Dev-only virtual module from @stylexjs/unplugin — fetches the compiled StyleX
// CSS and re-injects it on HMR. Imported dynamically, and only in DEV, by main.tsx.
declare module 'virtual:stylex:runtime';
