import * as stylex from '@stylexjs/stylex';
import {useEffect, useRef, useState} from 'react';

import {effortLabel} from '../lib/effort';
import {shortModelLabel} from '../lib/format';
import {channels, colors, radii, type} from '../theme/tokens.stylex';

export interface PickerModel {
  readonly id: string;
  readonly label: string;
  /** The effort levels offered for it; empty when it takes none. */
  readonly efforts: readonly string[];
}

interface Props {
  models: readonly PickerModel[] | undefined;
  model: string;
  /** The level the next turn runs at, already resolved; null when the model takes none. */
  effort: string | null;
  onModel: (id: string) => void;
  onEffort: (level: string) => void;
  /** A phone's footer: the trigger names the model as briefly as it can. */
  compact?: boolean;
}

/**
 * The composer's model control: one button naming the model and its effort,
 * opening a panel with the catalog above and the effort levels below.
 *
 * It replaced a native `<select>`, which can hold one choice. Effort is a
 * property of the model you picked — a model that takes none shows no row —
 * so the two are chosen in one place rather than as two dropdowns side by
 * side in a footer that has no room for a second.
 */
export function ModelPicker({models, model, effort, onModel, onEffort, compact = false}: Props) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const selectedRef = useRef<HTMLButtonElement | null>(null);
  const current = models?.find((m) => m.id === model);

  // Open on the current model, not the top of a catalog it may be far down.
  useEffect(() => {
    if (open) selectedRef.current?.scrollIntoView({block: 'nearest'});
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: MouseEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false);
    }
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);

  const levels = current?.efforts ?? [];

  return (
    <div {...stylex.props(picker.root)} ref={rootRef}>
      <button
        type="button"
        {...stylex.props(picker.trigger, open && picker.triggerOpen)}
        onClick={() => setOpen((v) => !v)}
        disabled={!models}
        aria-haspopup="dialog"
        aria-label="Model and effort"
        aria-expanded={open}
        title={models ? (current?.label ?? model) : 'Loading models…'}
      >
        <span {...stylex.props(picker.triggerModel)}>
          {models
            ? current
              ? compact
                ? shortModelLabel(current.label)
                : shortLabel(current.label)
              : model
            : 'Loading…'}
        </span>
        {effort && <span {...stylex.props(picker.triggerEffort)}>{effortLabel(effort)}</span>}
        <svg
          width="10"
          height="10"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2.5"
          strokeLinecap="round"
          strokeLinejoin="round"
          {...stylex.props(picker.chevron)}
        >
          <polyline points="6 9 12 15 18 9" />
        </svg>
      </button>

      {open && models && (
        <div {...stylex.props(picker.panel)} role="dialog" aria-label="Model and effort">
          <div {...stylex.props(picker.heading)}>Model</div>
          <div {...stylex.props(picker.list)} role="radiogroup" aria-label="Model">
            {models.map((m) => {
              const selected = m.id === model;
              return (
                <button
                  key={m.id}
                  ref={selected ? selectedRef : undefined}
                  type="button"
                  role="radio"
                  aria-checked={selected}
                  {...stylex.props(picker.option, selected && picker.optionSelected)}
                  onClick={() => {
                    onModel(m.id);
                    // A model with effort keeps the panel open for the row
                    // below; one without has nothing left to choose.
                    if (m.efforts.length === 0) setOpen(false);
                  }}
                  title={m.id}
                >
                  <span {...stylex.props(picker.optionLabel)}>{m.label}</span>
                  {selected && <CheckIcon />}
                </button>
              );
            })}
          </div>

          {levels.length > 0 && (
            <>
              <div {...stylex.props(picker.heading, picker.headingRule)}>Effort</div>
              <div {...stylex.props(picker.levels)} role="radiogroup" aria-label="Effort">
                {levels.map((level) => {
                  const selected = level === effort;
                  return (
                    <button
                      key={level}
                      type="button"
                      role="radio"
                      aria-checked={selected}
                      {...stylex.props(picker.level, selected && picker.levelSelected)}
                      onClick={() => {
                        onEffort(level);
                        setOpen(false);
                      }}
                    >
                      {effortLabel(level)}
                    </button>
                  );
                })}
              </div>
              <p {...stylex.props(picker.note)}>
                Higher effort thinks longer and spends more tokens.
              </p>
            </>
          )}
        </div>
      )}
    </div>
  );
}

/**
 * A label without its trailing "(Provider)": the footer has room for the
 * model's name and its effort, and the panel lists the full label anyway.
 * A phone drops the family name too (`shortModelLabel`).
 */
function shortLabel(label: string): string {
  return label.replace(/\s*\([^)]*\)\s*$/, '') || label;
}

function CheckIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

/**
 * The panel opens *upward*, as the debug popover does: the composer is the
 * bottom of the screen and there is nowhere below it to go.
 */
const picker = stylex.create({
  root: {
    position: 'relative',
    display: 'flex',
    minWidth: 0,
    // Must be able to shrink on a phone, or the send button is pushed past
    // the right edge once iOS forces 16px controls.
    flexShrink: {default: 0, '@media (max-width: 860px)': 1},
  },
  trigger: {
    display: 'flex',
    alignItems: 'center',
    gap: 5,
    minWidth: 0,
    // On a phone the row's free space is the cap: the keyboard hint beside
    // it gives way first.
    maxWidth: {default: 280, '@media (max-width: 860px)': 'none'},
    height: {default: 26, '@media (max-width: 860px)': 36},
    paddingInline: 7,
    borderStyle: 'none',
    borderRadius: radii.md,
    backgroundColor: {default: 'transparent', ':hover:not(:disabled)': colors.surface2},
    color: {default: colors.textDim, ':hover:not(:disabled)': colors.text},
    fontSize: type.tSmall,
    // The body face, not mono: a model name is a label you pick.
    fontFamily: type.body,
    cursor: {default: 'pointer', ':disabled': 'default'},
    transition: 'color 0.15s, background-color 0.15s',
  },
  triggerOpen: {color: colors.text, backgroundColor: colors.surface2},
  triggerModel: {minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'},
  triggerEffort: {
    flexShrink: 0,
    color: colors.textFaint,
    whiteSpace: 'nowrap',
    // A thin rule between the two names rather than a "·", which at 12px
    // reads as a stray mark.
    paddingInlineStart: 6,
    borderInlineStartWidth: 1,
    borderInlineStartStyle: 'solid',
    borderInlineStartColor: colors.border,
  },
  chevron: {flexShrink: 0, opacity: 0.7},
  panel: {
    position: 'absolute',
    insetBlockEnd: 'calc(100% + 6px)',
    insetInlineStart: 0,
    zIndex: 50,
    width: 'max-content',
    minWidth: 240,
    maxWidth: 'min(340px, calc(100vw - 32px))',
    backgroundColor: colors.bg,
    backgroundImage: `linear-gradient(180deg, rgba(${channels.tint}, 0.05), rgba(${channels.tint}, 0.02))`,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: colors.border,
    borderRadius: radii.md,
    paddingBlock: 6,
    boxShadow: `0 10px 34px rgba(${channels.shadow}, 0.4)`,
  },
  heading: {
    fontSize: type.tMicro,
    textTransform: 'uppercase',
    letterSpacing: '0.08em',
    color: colors.textFaint,
    paddingBlock: '4px 4px',
    paddingInline: 12,
  },
  headingRule: {
    marginBlockStart: 6,
    paddingBlockStart: 10,
    borderBlockStartWidth: 1,
    borderBlockStartStyle: 'solid',
    borderBlockStartColor: colors.border,
  },
  // A long custom catalog scrolls here rather than running off the top of
  // the screen.
  list: {
    display: 'flex',
    flexDirection: 'column',
    maxHeight: 'min(320px, 45vh)',
    overflowY: 'auto',
  },
  option: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    gap: 10,
    width: '100%',
    paddingBlock: {default: 6, '@media (max-width: 860px)': 10},
    paddingInline: 12,
    borderStyle: 'none',
    backgroundColor: {default: 'transparent', ':hover': colors.surface2},
    color: {default: colors.textDim, ':hover': colors.text},
    fontSize: type.tUi,
    fontFamily: type.body,
    textAlign: 'start',
    cursor: 'pointer',
  },
  optionSelected: {color: colors.text},
  optionLabel: {minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap'},
  levels: {display: 'flex', flexWrap: 'wrap', gap: 4, paddingInline: 10, paddingBlock: '2px 4px'},
  level: {
    flexGrow: 1,
    paddingBlock: {default: 4, '@media (max-width: 860px)': 8},
    paddingInline: 8,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.borderStrong},
    borderRadius: radii.sm,
    backgroundColor: 'transparent',
    color: {default: colors.textDim, ':hover': colors.text},
    fontSize: type.tSmall,
    fontFamily: type.body,
    whiteSpace: 'nowrap',
    cursor: 'pointer',
  },
  // Filled with ink, the one "this is on" the theme allows itself.
  levelSelected: {
    backgroundColor: colors.accent,
    borderColor: {default: colors.accent, ':hover': colors.accent},
    color: {default: colors.accentContrast, ':hover': colors.accentContrast},
  },
  note: {
    margin: 0,
    paddingInline: 12,
    paddingBlock: '6px 4px',
    fontSize: type.tSmall,
    color: colors.textFaint,
  },
});
