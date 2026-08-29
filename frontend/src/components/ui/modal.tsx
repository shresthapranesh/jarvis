/* ════════════════════════════════════════════════════════════════════
   The modal sheet, and the toggle switch that needed to become a component.
   ════════════════════════════════════════════════════════════════════ */
import * as stylex from '@stylexjs/stylex';

import {kf} from '../../theme/keyframes.stylex';
import {channels, colors, type} from '../../theme/tokens.stylex';

/**
 * The dimmed, blurred sheet behind any modal, and the raised panel on it.
 * Shared by ConfirmDialog, FormModal and the model-sync dialog.
 */
export const modal = stylex.create({
  backdrop: {
    position: 'fixed',
    inset: 0,
    zIndex: 200,
    backgroundColor: `rgba(${channels.shadow}, 0.55)`,
    backdropFilter: 'blur(4px)',
    WebkitBackdropFilter: 'blur(4px)',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    animationName: kf.fadeIn,
    animationDuration: '0.15s',
    animationTimingFunction: 'ease-out',
  },
  panel: {
    backgroundImage: `linear-gradient(180deg, rgba(${channels.tint}, 0.05), rgba(${channels.tint}, 0.02))`,
    backgroundColor: colors.bg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 3,
    padding: 22,
    width: 'calc(100% - 32px)',
    boxShadow: `0 18px 60px rgba(${channels.shadow}, 0.55)`,
    animationName: kf.confirmZoom,
    animationDuration: '0.18s',
    animationTimingFunction: 'ease-out',
  },
  title: {
    fontSize: type.tTitle,
    fontWeight: 500,
    color: colors.text,
    marginBlock: '0 4px',
    fontFamily: type.display,
  },
  subtitle: {fontSize: type.tUi, color: colors.textDim, lineHeight: 1.5, margin: 0},
  kbd: {
    fontFamily: type.mono,
    fontSize: type.tMicro,
    backgroundColor: colors.surface2,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: 2,
    paddingBlock: 1,
    paddingInline: 4,
  },
});

/**
 * Toggle switch.
 *
 * A component rather than a pair of classes: the old CSS drove the track from
 * `input:checked + .switch-track`, and StyleX has no sibling combinator. React
 * already holds `checked`, so the state is read from props and the visually
 * hidden input stays purely for semantics and keyboard focus.
 */
export function Switch({
  checked,
  onChange,
  disabled = false,
  label,
  title,
}: {
  checked: boolean;
  onChange: (next: boolean) => void;
  disabled?: boolean;
  label?: React.ReactNode;
  title?: string;
}) {
  return (
    <label {...stylex.props(sw.root, label != null && sw.rootLabelled)} title={title}>
      <input
        {...stylex.props(sw.input)}
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      <span
        {...stylex.props(
          sw.track,
          checked && sw.trackOn,
          disabled && sw.trackDisabled,
          checked && sw.thumbOn,
        )}
        aria-hidden="true"
      />
      {label}
    </label>
  );
}

const sw = stylex.create({
  root: {
    display: 'inline-flex',
    alignItems: 'center',
    gap: 8,
    cursor: 'pointer',
    fontSize: type.tUi,
    color: colors.textDim,
    // The track's focus ring is driven off the hidden input's focus, which only
    // the shared parent can observe.
    '--switch-outline': {default: 'none', ':focus-within': `2px solid ${colors.accent}`},
  },
  rootLabelled: {gap: 8},
  input: {position: 'absolute', opacity: 0, width: 0, height: 0},
  track: {
    position: 'relative',
    width: 30,
    height: 17,
    borderRadius: 3,
    backgroundColor: colors.surface3,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.borderStrong,
    transition: 'background 0.15s ease, border-color 0.15s ease',
    flexShrink: 0,
    outline: 'var(--switch-outline)',
    outlineOffset: 2,
    '::after': {
      content: '',
      position: 'absolute',
      insetBlockStart: 2,
      insetInlineStart: 2,
      width: 11,
      height: 11,
      borderRadius: '50%',
      backgroundColor: colors.textDim,
      transition: 'transform 0.15s ease, background 0.15s ease',
    },
  },
  trackOn: {
    backgroundColor: `rgba(${channels.signalLive}, 0.22)`,
    borderColor: `rgba(${channels.signalLive}, 0.55)`,
  },
  thumbOn: {'::after': {transform: 'translateX(13px)', backgroundColor: colors.ok}},
  trackDisabled: {opacity: 0.5, cursor: 'not-allowed'},
});
