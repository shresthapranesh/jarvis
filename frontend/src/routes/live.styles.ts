import * as stylex from '@stylexjs/stylex';

import {kf} from '../theme/keyframes.stylex';
import {channels, colors, type} from '../theme/tokens.stylex';

/* ── Styles for routes/live.tsx ────────────────────────────────────────
   Voice mode: a header of engine toggles, the transcript of turns, and
   the call controls pinned to the bottom. */

export const live = stylex.create({
  page: {display: 'flex', flexDirection: 'column', height: '100%', overflow: 'hidden'},

  header: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'space-between',
    paddingBlockStart: 14,
    paddingBlockEnd: 12,
    paddingInline: 20,
    borderBlockEndWidth: 1,
    borderBlockEndStyle: 'solid',
    borderBlockEndColor: colors.border,
    flexShrink: 0,
  },
  title: {
    fontSize: type.tBody,
    fontWeight: 600,
    display: 'flex',
    alignItems: 'center',
    gap: 7,
    // The recording dot. A ::before rather than an element because it is
    // ornament, not content.
    '::before': {
      content: '',
      display: 'inline-block',
      width: 7,
      height: 7,
      borderRadius: '50%',
      backgroundColor: colors.danger,
      animationName: kf.pulseDot,
      animationDuration: '1.8s',
      animationTimingFunction: 'ease-in-out',
      animationIterationCount: 'infinite',
    },
  },
  headerControls: {display: 'flex', alignItems: 'center', gap: 10},

  modelInput: {
    backgroundColor: 'transparent',
    borderStyle: 'none',
    outline: 'none',
    color: {default: colors.textDim, ':focus': colors.text},
    fontSize: type.tSmall,
    fontFamily: type.mono,
    maxWidth: 200,
  },
  connDot: {
    width: 7,
    height: 7,
    borderRadius: '50%',
    backgroundColor: '#555',
    transition: 'background 0.4s',
    flexShrink: 0,
  },
  connDotOk: {backgroundColor: colors.ok},

  /** The Whisper / Browser and TTS pills. */
  toggle: {
    display: 'flex',
    alignItems: 'center',
    gap: 5,
    backgroundColor: 'transparent',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.accentDim},
    borderRadius: 2,
    color: {default: colors.textDim, ':hover': colors.accent},
    fontSize: type.tMicro,
    fontFamily: 'inherit',
    paddingBlock: 3,
    paddingInline: 8,
    cursor: 'pointer',
    transition: 'color 0.15s, border-color 0.15s, background 0.15s',
  },
  toggleActive: {
    backgroundColor: colors.accentDim,
    color: {default: colors.accent, ':hover': colors.accent},
    borderColor: {default: colors.accentDim, ':hover': colors.accentDim},
  },

  turns: {
    flex: 1,
    overflowY: 'auto',
    paddingBlockStart: 24,
    paddingBlockEnd: 12,
    paddingInline: 20,
    display: 'flex',
    flexDirection: 'column',
    gap: 18,
    '::-webkit-scrollbar': {width: 6},
    '::-webkit-scrollbar-thumb': {
      backgroundColor: `rgba(${channels.tint}, 0.12)`,
      borderRadius: 2,
    },
  },
  empty: {textAlign: 'center', color: colors.textDim, fontSize: type.tBody, margin: 'auto'},

  turn: {
    display: 'flex',
    flexDirection: 'column',
    gap: 5,
    maxWidth: 760,
    width: '100%',
    marginInline: 'auto',
  },
  turnUser: {alignItems: 'flex-end'},
  turnAgent: {alignItems: 'flex-start'},
  userBubble: {
    backgroundColor: colors.userBg,
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: `rgba(${channels.accent}, 0.2)`,
    borderStartStartRadius: 3,
    borderStartEndRadius: 3,
    borderEndEndRadius: 2,
    borderEndStartRadius: 3,
    paddingBlock: 10,
    paddingInline: 14,
    maxWidth: '72%',
    lineHeight: 1.55,
    whiteSpace: 'pre-wrap',
    wordBreak: 'break-word',
    fontSize: type.tBody,
  },
  /** The steps count under an agent turn — a label, not a control. */
  stepsBadge: {alignSelf: 'flex-start', cursor: 'default'},
  thinking: {
    display: 'flex',
    alignItems: 'center',
    gap: 8,
    color: colors.textDim,
    fontSize: type.tBody,
  },

  interim: {
    textAlign: 'center',
    fontSize: type.tUi,
    fontStyle: 'italic',
    color: colors.textDim,
    paddingBlock: 4,
    paddingInline: 20,
    flexShrink: 0,
  },
  statusBar: {
    textAlign: 'center',
    fontSize: type.tUi,
    color: colors.textDim,
    paddingBlockStart: 4,
    paddingBlockEnd: 2,
    paddingInline: 20,
    flexShrink: 0,
  },
  controls: {
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    paddingBlockStart: 14,
    paddingBlockEnd: 22,
    paddingInline: 20,
    flexShrink: 0,
  },

  unsupported: {
    display: 'flex',
    flexDirection: 'column',
    alignItems: 'center',
    justifyContent: 'center',
    gap: 12,
    height: '100%',
    color: colors.textDim,
    textAlign: 'center',
    padding: 40,
  },
  unsupportedTitle: {fontSize: type.tTitle, color: colors.text},
  unsupportedBody: {fontSize: type.tBody, lineHeight: 1.6},
});

/** The call bar: start, mute, hang up, and the status orb between them. */
export const call = stylex.create({
  start: {
    display: 'flex',
    alignItems: 'center',
    gap: 9,
    paddingBlock: 13,
    paddingInline: 28,
    borderRadius: 2,
    borderStyle: 'none',
    backgroundImage: `linear-gradient(135deg, ${colors.accentStrong}, ${colors.accent})`,
    color: colors.accentContrast,
    fontSize: type.tBody,
    fontFamily: 'inherit',
    fontWeight: 500,
    cursor: {default: 'pointer', ':disabled': 'not-allowed'},
    opacity: {default: 1, ':disabled': 0.4},
    boxShadow: {
      default: `0 4px 20px rgba(${channels.accent}, 0.4)`,
      ':hover:not(:disabled)': `0 6px 28px rgba(${channels.accent}, 0.55)`,
    },
    transform: {default: null, ':hover:not(:disabled)': 'translateY(-1px)'},
    transition: 'box-shadow 0.2s, transform 0.15s',
  },
  active: {display: 'flex', alignItems: 'center', gap: 18},

  /**
   * The orb keys off `data-state`, which the component already sets for
   * assistive tech — so the attribute selector stays rather than becoming a
   * second, redundant source of the same fact.
   */
  orb: {
    width: 14,
    height: 14,
    borderRadius: '50%',
    backgroundColor: colors.border,
    flexShrink: 0,
    transition: 'background 0.3s',
  },
  orbListening: {
    backgroundColor: colors.ok,
    animationName: kf.orbPulse,
    animationDuration: '1.4s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  orbThinking: {
    backgroundColor: colors.accent,
    animationName: kf.orbSpinRing,
    animationDuration: '1s',
    animationTimingFunction: 'linear',
    animationIterationCount: 'infinite',
  },
  orbSpeaking: {
    backgroundColor: '#a78bfa',
    animationName: kf.orbPulse,
    animationDuration: '0.7s',
    animationTimingFunction: 'ease-in-out',
    animationIterationCount: 'infinite',
  },
  orbMuted: {backgroundColor: '#555'},
  orbIdle: {backgroundColor: colors.ok, opacity: 0.4},

  btn: {
    width: 52,
    height: 52,
    borderRadius: '50%',
    borderWidth: 1,
    borderStyle: 'solid',
    borderColor: {default: colors.border, ':hover': colors.accentDim},
    backgroundColor: colors.surface,
    color: {default: colors.textDim, ':hover': colors.text},
    cursor: 'pointer',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    transition: 'background 0.15s, border-color 0.15s, color 0.15s',
    flexShrink: 0,
  },
  btnMuted: {
    backgroundColor: `rgba(${channels.danger}, 0.12)`,
    borderColor: {default: '#5c1515', ':hover': '#5c1515'},
    color: {default: colors.danger, ':hover': colors.danger},
  },
  btnEnd: {
    backgroundColor: {default: colors.danger, ':hover': '#dc2626'},
    borderColor: {default: colors.danger, ':hover': '#dc2626'},
    color: {default: '#fff', ':hover': '#fff'},
  },
});

export function orbStyle(state: string) {
  if (state === 'listening') return call.orbListening;
  if (state === 'thinking') return call.orbThinking;
  if (state === 'speaking') return call.orbSpeaking;
  if (state === 'muted') return call.orbMuted;
  if (state === 'idle') return call.orbIdle;
  return null;
}
