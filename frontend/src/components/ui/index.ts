/* ════════════════════════════════════════════════════════════════════
   The shared vocabulary — the styles more than a couple of screens use.

   These were the classes that made styles.css a de-facto design system
   without ever being declared one: `artifact-btn` on 15 screens,
   `memory-empty` on 17, `auto-form-*` on 11. StyleX styles compose across
   modules, so they live here as exported `stylex.create` objects and are
   merged at each call site:

     <button {...stylex.props(btn.base, btn.primary)}>

   Two things follow from that. Order matters — later arguments win, so
   variants come after `base`. And an unused variant is dropped from the
   bundle, which a global class could never be.

   Split by what the styles are for; import from `./ui`, not the parts.
   ════════════════════════════════════════════════════════════════════ */
export {btn, chipBtn, closeBtn, iconBtn} from './buttons';
export {codeField, field} from './forms';
export {badge, kindBadge, kindBadgeStyle, page} from './page';
export {ResizeHandle, useResizableWidth} from './resize';
export {errorBubble, prose, stream, ThinkingDots, turn, worker} from './run';
export {modal, Switch} from './modal';
