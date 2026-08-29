import * as stylex from '@stylexjs/stylex';
import {useEffect, useMemo, useRef, useState} from 'react';

import type {WorkerInfo} from '../hooks/useTaskEvents';
import {compactNumber} from '../lib/format';
import {messageAnchorId, messageExcerpt} from '../lib/thread';
import type {Message, Step} from '../lib/types';
import {foot, mini, node as nodeStyles, peek, rail, track} from './ThreadSpine.styles';

interface Budget {
  inputTokens: number;
  outputTokens: number;
  totalTokens: number;
  llmCalls: number;
  toolCalls: number;
}

interface Props {
  messages: Message[];
  steps: Step[];
  workers?: WorkerInfo[];
  artifactCount?: number;
  isLive?: boolean;
  budget?: Budget | null;
  /** Opens the full ActivitySidebar — the footer's job, not a dot's. */
  onExpand: () => void;
  /** Scrolls the thread to a turn. Owned by the route: it holds the scroller
   *  and the pin-to-bottom flag a jump has to release. */
  onJump: (messageId: string) => void;
}

// The rail is a *map of the conversation*: one dot per turn, in order. Its
// dots navigate the thread; the run's step trace lives behind "Open details".
// Collapsed it is 30px, so only the tail of a long thread fits as marks.
const MAX_MARKS = 18;

type SpineKind = 'user' | 'assistant' | 'blocked' | 'error';

function messageKind(message: Message): SpineKind {
  if (message.role === 'user') return 'user';
  if (message.status === 'blocked') return 'blocked';
  if (message.status === 'error') return 'error';
  return 'assistant';
}

const KIND_LABEL: Record<SpineKind, string> = {
  user: 'You',
  assistant: 'Assistant',
  blocked: 'Blocked',
  error: 'Failed',
};

export function ThreadSpine({
  messages,
  steps,
  workers = [],
  artifactCount = 0,
  isLive = false,
  budget,
  onExpand,
  onJump,
}: Props) {
  const trackRef = useRef<HTMLDivElement | null>(null);

  // A live run always shows the full column; a settled one is a 30px trace
  // until you point at it. Hover is state rather than a `:hover` rule because
  // the two are separate renders — see the note in ThreadSpine.styles.ts. The
  // rail is `display: none` below `bp.wide`, so there is no pointerless
  // viewport to strand here; on a touchscreen a tap expands it first.
  const [hovered, setHovered] = useState(false);
  const expanded = isLive || hovered;

  // Which turn the reader is actually looking at, so the rail reads as a
  // position indicator and not just a list. Driven by the anchors the bubbles
  // render, since the thread scroller is owned by the route, not by us.
  const [activeId, setActiveId] = useState<string | null>(null);
  const ids = useMemo(() => messages.map((m) => m.id), [messages]);

  useEffect(() => {
    const anchors = ids
      .map((id) => document.getElementById(messageAnchorId(id)))
      .filter((el): el is HTMLElement => el !== null);
    if (anchors.length === 0) return;

    // Topmost intersecting turn wins: while a long answer fills the viewport
    // it stays selected, and the rail does not flicker between neighbours.
    const seen = new Set<string>();
    const observer = new IntersectionObserver(
      (entries) => {
        for (const e of entries) {
          if (e.isIntersecting) seen.add(e.target.id);
          else seen.delete(e.target.id);
        }
        const first = anchors.find((el) => seen.has(el.id));
        if (first) setActiveId(first.id.replace(/^msg-/, ''));
      },
      {rootMargin: '-45% 0px -45% 0px'},
    );
    anchors.forEach((el) => observer.observe(el));
    return () => observer.disconnect();
  }, [ids]);

  // Newest turn is at the bottom; follow it while the run is live.
  useEffect(() => {
    if (!isLive) return;
    const el = trackRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length, isLive]);

  // The hover card is fixed-positioned from the dot's own rect: the track
  // scrolls, so a card nested inside it would be clipped at the rail's edge.
  const [peeked, setPeeked] = useState<{top: number; message: Message} | null>(null);

  function showPeek(el: HTMLElement, message: Message) {
    const r = el.getBoundingClientRect();
    setPeeked({top: r.top + r.height / 2, message});
  }

  function jumpTo(id: string) {
    onJump(id);
    setActiveId(id);
  }

  if (!expanded) {
    return (
      <aside
        {...stylex.props(rail.root, rail.rootCollapsed)}
        aria-label={`Conversation map — ${messages.length} message${messages.length === 1 ? '' : 's'}`}
        onMouseEnter={() => setHovered(true)}
        onClick={() => setHovered(true)}
      >
        <div {...stylex.props(mini.track)}>
          {messages.slice(-MAX_MARKS).map((m) => (
            <span
              key={m.id}
              {...stylex.props(
                nodeStyles.mark,
                markForKind(messageKind(m)),
                m.id === activeId && nodeStyles.markCurrent,
              )}
              aria-hidden="true"
            />
          ))}
        </div>
        <span {...stylex.props(mini.count)}>{messages.length}</span>
      </aside>
    );
  }

  return (
    <aside
      {...stylex.props(rail.root)}
      aria-label="Conversation map"
      onMouseLeave={() => {
        setHovered(false);
        setPeeked(null);
      }}
    >
      <header {...stylex.props(rail.head)}>
        <span {...stylex.props(rail.eyebrow)}>Thread</span>
        <span {...stylex.props(rail.state, isLive && rail.stateLive)}>
          {isLive ? 'live' : `${messages.length}`}
        </span>
      </header>

      <div {...stylex.props(track.root)} ref={trackRef} onScroll={() => setPeeked(null)}>
        {messages.length === 0 && !isLive && <p {...stylex.props(track.empty)}>No messages yet.</p>}

        {/* The connecting hairline lives on this wrapper, not the scroll
            container, so it ends at the last node instead of dangling. */}
        <div {...stylex.props(track.nodes)}>
          {messages.map((m) => {
            const kind = messageKind(m);
            const gist = messageExcerpt(m.content, 90);
            return (
              <button
                key={m.id}
                {...stylex.props(nodeStyles.root, m.id === activeId && nodeStyles.current)}
                onClick={() => jumpTo(m.id)}
                onMouseEnter={(e) => showPeek(e.currentTarget, m)}
                onFocus={(e) => showPeek(e.currentTarget, m)}
                onMouseLeave={() => setPeeked(null)}
                onBlur={() => setPeeked(null)}
                aria-label={`Jump to ${KIND_LABEL[kind]} message`}
                aria-current={m.id === activeId ? 'true' : undefined}
              >
                <span
                  {...stylex.props(
                    nodeStyles.mark,
                    markForKind(kind),
                    m.id === activeId && nodeStyles.markCurrent,
                  )}
                  aria-hidden="true"
                />
                <span {...stylex.props(nodeStyles.label)}>{gist || KIND_LABEL[kind]}</span>
              </button>
            );
          })}

          {isLive && (
            <div {...stylex.props(nodeStyles.root, nodeStyles.pending)}>
              <span {...stylex.props(nodeStyles.mark, nodeStyles.markActive)} aria-hidden="true" />
              <span {...stylex.props(nodeStyles.label)}>replying…</span>
            </div>
          )}
        </div>
      </div>

      {peeked && (
        <div
          {...stylex.props(peek.card)}
          style={{top: clampPeek(peeked.top)}}
          role="tooltip"
          aria-hidden="true"
        >
          <span {...stylex.props(peek.role, roleStyle(messageKind(peeked.message)))}>
            {KIND_LABEL[messageKind(peeked.message)]}
          </span>
          <p {...stylex.props(peek.text)}>
            {messageExcerpt(peeked.message.content) || '(no text)'}
          </p>
        </div>
      )}

      <footer {...stylex.props(foot.root)}>
        <dl {...stylex.props(foot.stats)}>
          <div {...stylex.props(foot.stat)}>
            <dt {...stylex.props(foot.key)}>steps</dt>
            <dd {...stylex.props(foot.value)}>{steps.length}</dd>
          </div>
          {workers.filter((w) => w.status === 'running').length > 0 && (
            <div {...stylex.props(foot.stat)}>
              <dt {...stylex.props(foot.key)}>workers</dt>
              <dd {...stylex.props(foot.value, foot.valueWorkers)}>
                {workers.filter((w) => w.status === 'running').length}
              </dd>
            </div>
          )}
          {artifactCount > 0 && (
            <div {...stylex.props(foot.stat)}>
              <dt {...stylex.props(foot.key)}>artifacts</dt>
              <dd {...stylex.props(foot.value)}>{artifactCount}</dd>
            </div>
          )}
          {budget && budget.totalTokens > 0 && (
            <div {...stylex.props(foot.stat)}>
              <dt {...stylex.props(foot.key)}>tokens</dt>
              <dd {...stylex.props(foot.value)}>{compactNumber(budget.totalTokens)}</dd>
            </div>
          )}
        </dl>
        <button {...stylex.props(foot.expand)} onClick={onExpand}>
          Open details
        </button>
      </footer>
    </aside>
  );
}

/** Keep the hover card fully on screen when a dot sits near either edge. */
function clampPeek(centerY: number): number {
  const half = 60;
  return Math.min(Math.max(centerY, half + 8), window.innerHeight - half - 8);
}

/** Turn kind → mark colour. Declared beside the styles it selects from. */
function markForKind(kind: SpineKind) {
  switch (kind) {
    case 'user':
      return nodeStyles.markUser;
    case 'blocked':
      return nodeStyles.markBlocked;
    case 'error':
      return nodeStyles.markError;
    default:
      return nodeStyles.markAssistant;
  }
}

function roleStyle(kind: SpineKind) {
  switch (kind) {
    case 'user':
      return peek.roleUser;
    case 'blocked':
      return peek.roleBlocked;
    case 'error':
      return peek.roleError;
    default:
      return peek.roleAssistant;
  }
}
