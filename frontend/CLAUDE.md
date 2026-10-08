# frontend/ — React 19 + TanStack Router + Relay + StyleX + Vite

Always `pnpm`, never npm.

```bash
pnpm dev        # vite on :5173 + relay-compiler --watch
pnpm relay      # regenerate src/__generated__
pnpm schema     # regenerate schema.graphql from the server (`jarvis-edge --print-schema`)
pnpm typecheck  # relay + tsc -b
pnpm build      # relay + vite build → ../static/dist/
```

- **Relay**: `tsc` and `vite build` need `src/__generated__/`, so run `pnpm relay` after changing any `graphql` literal, and `pnpm schema` first after changing the server's schema (the dev watcher doesn't see the Rust side). Never edit `__generated__/`.
- Relay keys records by `id` alone, regardless of type. Don't select `id` on a non-node type whose id can collide with a node's — alias it (see the model sync query).
- Operations live as per-operation modules in `src/relay/`; convert ids with `encodeGlobalId` / `decodeGlobalId` (`relay/globalId.ts`).
- **Routes**: file-based in `src/routes/` (`createFileRoute('/path')`). `routeTree.gen.ts` is generated — never edit it.
- **REST helpers** go in `src/lib/api.ts`; dev proxying in `vite.config.ts` (`/graphql`, `/health`, `/ws/browser`, `/artifacts`, `/server-logs`).
- **Formatting**: `pnpm fmt` (oxfmt) rewrites many untouched files — format only the files you changed.
- Test mobile layouts with DevTools device emulation, not window resizing.

## Live data
- `useTaskEvents`, `useAutomationRunEvents`, `useWorkflowRunEvents`, `useBoardTaskEvents` wrap the subscriptions. `useTaskEvents` resets on every new task id, so anything that must outlive a run (e.g. the Browser button) comes from a query, not events.
- Queued mid-run messages: `queued_*` events trigger a refetch; the DB rows are the source of truth. `orderDeliveredAboveReply` (`routes/c.$id.tsx`) lifts `delivered` messages above their reply.
- Approvals: blocking ones (per-tool gates, with `approvalId`) show in `InterruptPrompt`, answered with `resolveApproval`; deferred ones come from `usePendingApprovals` polling and must not set `pendingInterrupt`.
- Per-message perf numbers are in the ⓘ popover (`DebugInfo` in `MessageBubble.tsx`); unmeasured values are omitted, never shown as 0.

## Styling (StyleX)
No UI library, no global stylesheet beyond `src/base.css`.
- Tokens in `src/theme/` (`tokens.stylex.ts`, `themes.stylex.ts`, `keyframes.stylex.ts`). **Every export of a `.stylex.ts` file becomes a hashed StyleX constant** — raw CSS strings go through `stylex.defineConsts` (`css`, `bp`).
- Shorthands are split: write `backgroundColor`/`backgroundImage`, `animationName`/`animationDuration`, never `background`/`animation`.
- No combinators: a parent styling a child becomes a prop or a CSS custom property the child reads. Responsive rules are per-property conditions (`{default: …, '@media (…)': …}`).
- Styles colocated at the bottom of the component, in a sibling `*.styles.ts` when large, or in `components/ui/` when shared (import from `./ui`).
- `base.css` holds only what StyleX can't: reset, pre-paint theme block, view-transition rules, `[data-md]` markdown rules, `@xyflow/react` overrides (prefixed `:root`). The `--pp-*` literals must track `colors.bg` / `colors.text`.
- The theme class is applied at boot by `theme/applyTheme.ts`; `index.html` only stamps `data-theme`.
- `vite.config.ts` sets `cssInjectionTarget` explicitly; `.browserslistrc` must target browsers with native `light-dark()`.
