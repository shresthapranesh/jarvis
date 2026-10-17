import * as stylex from '@stylexjs/stylex';
import {type ReactNode, useEffect, useState} from 'react';
import {createPortal} from 'react-dom';

import {channels, colors, type} from '../../theme/tokens.stylex';

export interface OverflowItem {
  label: string;
  icon?: ReactNode;
  onSelect: () => void;
  /** Destructive: tinted red on hover. */
  danger?: boolean;
}

interface Props {
  items: OverflowItem[];
  /** The button's tooltip and accessible name. */
  label?: string;
  /** Extra styles for the trigger, e.g. to show it only on a narrow screen. */
  style?: stylex.StyleXStyles;
}

/**
 * A "…" button that opens a short list of actions — the place a row's less
 * frequent actions go when there is no room to show them all as buttons.
 * The list is portalled to the body so a card's `overflow: hidden` can't clip it.
 */
export function OverflowMenu({items, label = 'More actions', style}: Props) {
  const [pos, setPos] = useState<{x: number; y: number} | null>(null);

  useEffect(() => {
    if (!pos) return;
    const close = () => setPos(null);
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', onKey);
    window.addEventListener('resize', close);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', onKey);
      window.removeEventListener('resize', close);
    };
  }, [pos]);

  return (
    <>
      <button
        type="button"
        {...stylex.props(styles.trigger, pos && styles.triggerOpen, style)}
        title={label}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={pos !== null}
        onPointerDown={(e) => e.stopPropagation()}
        onClick={(e) => {
          e.stopPropagation();
          const r = e.currentTarget.getBoundingClientRect();
          setPos(pos ? null : {x: r.right, y: r.bottom + 4});
        }}
      >
        <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
          <circle cx="5" cy="12" r="1.8" />
          <circle cx="12" cy="12" r="1.8" />
          <circle cx="19" cy="12" r="1.8" />
        </svg>
      </button>
      {pos &&
        createPortal(
          <div
            role="menu"
            {...stylex.props(styles.menu)}
            style={{top: pos.y, left: pos.x}}
            onPointerDown={(e) => e.stopPropagation()}
            onClick={(e) => e.stopPropagation()}
          >
            {items.map((item) => (
              <button
                key={item.label}
                type="button"
                role="menuitem"
                {...stylex.props(styles.item, item.danger && styles.itemDanger)}
                onClick={() => {
                  setPos(null);
                  item.onSelect();
                }}
              >
                {item.icon && <span {...stylex.props(styles.icon)}>{item.icon}</span>}
                {item.label}
              </button>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}

/**
 * The two halves of the usual split: a row's secondary buttons show on a wide
 * screen and fold into the menu on a phone. Both assume a flex item.
 */
export const overflowSplit = stylex.create({
  wideOnly: {display: {default: 'flex', '@media (max-width: 600px)': 'none'}},
  narrowOnly: {display: {default: 'none', '@media (max-width: 600px)': 'flex'}},
});

const styles = stylex.create({
  trigger: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    width: 30,
    height: 30,
    flexShrink: 0,
    padding: 0,
    backgroundColor: {default: colors.surface, ':hover': colors.surface2},
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: 'transparent',
    borderRadius: 2,
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
  },
  triggerOpen: {color: colors.text, backgroundColor: colors.surface2},
  menu: {
    position: 'fixed',
    transform: 'translateX(-100%)',
    minWidth: 150,
    backgroundColor: colors.glassBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.borderStrong,
    borderRadius: 3,
    boxShadow: `0 8px 28px rgba(${channels.shadow}, 0.45)`,
    padding: 4,
    zIndex: 200,
    display: 'flex',
    flexDirection: 'column',
  },
  item: {
    display: 'flex',
    alignItems: 'center',
    gap: 10,
    minHeight: 36,
    paddingInline: 10,
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    borderStyle: 'none',
    borderRadius: 2,
    color: colors.text,
    fontSize: type.tUi,
    fontFamily: 'inherit',
    textAlign: 'left',
    cursor: 'pointer',
    '--menu-icon-color': colors.textDim,
  },
  itemDanger: {
    backgroundColor: {default: 'transparent', ':hover': `rgba(${channels.danger}, 0.1)`},
    color: {default: colors.text, ':hover': colors.danger},
    '--menu-icon-color': {default: colors.textDim, ':hover': colors.danger},
  },
  icon: {display: 'inline-flex', flexShrink: 0, color: 'var(--menu-icon-color)'},
});
