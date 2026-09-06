import * as stylex from '@stylexjs/stylex';
import {marked} from 'marked';
import {useEffect, useMemo, useRef, useState} from 'react';

function SpeakerIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="currentColor"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg
      {...stylex.props(stream.spinner)}
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
    </svg>
  );
}

import type {WorkerInfo} from '../hooks/useTaskEvents';
import {describeStep, getStepPreview} from '../lib/steps';
import {messageAnchorId} from '../lib/thread';
import type {ArtifactCard, Message, Step} from '../lib/types';
import {MessageArtifacts} from './MessageArtifacts';
import {actions, bubble, debug, media, safety, working} from './MessageBubble.styles';
import {chipBtn, prose, stream, ThinkingDots, turn} from './ui';
import {WorkerPanel} from './WorkerPanel';

// Rendering for historical messages persisted with status "blocked" by the
// old safety gates (feature removed; rows may still exist in the DB).
function SafetyBanner({layer}: {layer: 'input' | 'output'}) {
  const layerLabel = layer === 'input' ? 'Input blocked' : 'Output redacted';
  return (
    <div {...stylex.props(safety.banner)}>
      <span {...stylex.props(safety.label)}>⚠ {layerLabel}</span>
    </div>
  );
}

type ContentPart =
  | {type: 'text'; text: string}
  | {type: 'image'; name: string; size: number; mimeType: string}
  | {type: 'audio'; name: string; size: number; mimeType: string}
  | {type: 'video'; name: string; size: number; mimeType: string}
  | {type: 'document'; name: string; size: number; mimeType: string};

function parseMultimodal(raw: string): ContentPart[] | null {
  try {
    const parts = JSON.parse(raw);
    if (Array.isArray(parts) && parts.length > 0 && parts[0]?.type) return parts as ContentPart[];
  } catch {}
  return null;
}

function formatRate(tps: number) {
  return tps >= 100 ? String(Math.round(tps)) : tps.toFixed(1);
}

function formatMs(ms: number) {
  return ms >= 1000 ? `${(ms / 1000).toFixed(2)}s` : `${Math.round(ms)}ms`;
}

/** Wall clock for the whole turn: what the user actually waited through. */
function formatDuration(ms: number) {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const mins = Math.floor(ms / 60_000);
  const secs = Math.round((ms % 60_000) / 1000);
  return `${mins}m ${String(secs).padStart(2, '0')}s`;
}

function InfoIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="M12 16v-5" />
      <path d="M12 8h.01" />
    </svg>
  );
}

type DebugRow = {label: string; value: string; hint?: string};

/** The rows the panel shows, in reading order. Anything unmeasured is dropped
 *  rather than rendered as 0 — a missing rate means "couldn't be split", which
 *  is not the same as slow. */
function debugRows(message: Message): DebugRow[] {
  const rows: DebugRow[] = [];
  if (message.duration_ms != null) {
    rows.push({
      label: 'Total time',
      value: formatDuration(message.duration_ms),
      hint: 'Wall clock for the turn — includes tools, retrieval and any wait for approval.',
    });
  }
  if (message.llm_ms != null) {
    rows.push({
      label: 'In LLM calls',
      value: formatMs(message.llm_ms),
      hint: 'Summed round trips. Excludes tools and retrieval, so always less than total time.',
    });
  }
  if (message.ttft_ms != null) {
    rows.push({
      label: 'First token',
      value: formatMs(message.ttft_ms),
      hint: 'Time to the first token of the run’s first LLM call.',
    });
  }
  if (message.input_tokens != null || message.output_tokens != null) {
    rows.push({
      label: 'Tokens',
      value: `↑ ${(message.input_tokens ?? 0).toLocaleString()}  ↓ ${(message.output_tokens ?? 0).toLocaleString()}`,
      hint: 'Input (full context sent, summed over every call) and output.',
    });
  }
  if (message.prefill_tps != null) {
    rows.push({
      label: 'Prompt processing',
      value: `${formatRate(message.prefill_tps)} tok/s`,
      hint: 'Prefill throughput, cache-read tokens excluded.',
    });
  }
  if (message.eval_tps != null) {
    rows.push({
      label: 'Generation',
      value: `${formatRate(message.eval_tps)} tok/s`,
      hint: 'Decode throughput.',
    });
  }
  if (message.model) rows.push({label: 'Model', value: message.model});
  return rows;
}

/**
 * Timing, token counts and throughput, folded behind one icon.
 *
 * These used to sit inline as two always-on badges, which put five numbers
 * under every settled turn for the sake of the rare moment anyone reads them.
 * Nothing here is needed to read the answer, so the row keeps only the icon.
 */
function DebugInfo({message}: {message: Message}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLSpanElement | null>(null);
  const rows = useMemo(() => debugRows(message), [message]);

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

  if (rows.length === 0) return null;

  return (
    <span {...stylex.props(debug.root)} ref={rootRef}>
      <button
        {...stylex.props(actions.copy, open && actions.copyActive)}
        onClick={() => setOpen((v) => !v)}
        title="Debug info"
        aria-label="Debug info"
        aria-expanded={open}
        type="button"
      >
        <InfoIcon />
      </button>
      {open && (
        <div {...stylex.props(debug.panel)} role="dialog" aria-label="Debug info">
          <div {...stylex.props(debug.heading)}>Debug info</div>
          <dl {...stylex.props(debug.list)}>
            {rows.map((row) => (
              <div key={row.label} {...stylex.props(debug.row)} title={row.hint}>
                <dt {...stylex.props(debug.label)}>{row.label}</dt>
                <dd {...stylex.props(debug.value)}>{row.value}</dd>
              </div>
            ))}
          </dl>
        </div>
      )}
    </span>
  );
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function MultimodalUserContent({parts}: {parts: ContentPart[]}) {
  return (
    <div {...stylex.props(media.stack)}>
      {parts.map((part, i) => {
        if (part.type === 'text') {
          return part.text ? (
            <p key={i} {...stylex.props(media.text)}>
              {part.text}
            </p>
          ) : null;
        }
        if (part.type === 'image') {
          return (
            <div key={i} {...stylex.props(media.chip)}>
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <rect x="3" y="3" width="18" height="18" rx="2" />
                <circle cx="8.5" cy="8.5" r="1.5" />
                <polyline points="21 15 16 10 5 21" />
              </svg>
              <span>{part.name}</span>
              <span {...stylex.props(media.size)}>{formatBytes(part.size)}</span>
            </div>
          );
        }
        if (part.type === 'audio') {
          return (
            <div key={i} {...stylex.props(media.chip)}>
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M9 18V5l12-2v13" />
                <circle cx="6" cy="18" r="3" />
                <circle cx="18" cy="16" r="3" />
              </svg>
              <span>{part.name}</span>
              <span {...stylex.props(media.size)}>{formatBytes(part.size)}</span>
            </div>
          );
        }
        if (part.type === 'video') {
          return (
            <div key={i} {...stylex.props(media.chip)}>
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <polygon points="23 7 16 12 23 17 23 7" />
                <rect x="1" y="5" width="15" height="14" rx="2" ry="2" />
              </svg>
              <span>{part.name}</span>
              <span {...stylex.props(media.size)}>{formatBytes(part.size)}</span>
            </div>
          );
        }
        if (part.type === 'document') {
          return (
            <div key={i} {...stylex.props(media.chip)}>
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <line x1="16" y1="13" x2="8" y2="13" />
                <line x1="16" y1="17" x2="8" y2="17" />
              </svg>
              <span>{part.name}</span>
              <span {...stylex.props(media.size)}>{formatBytes(part.size)}</span>
            </div>
          );
        }
        return null;
      })}
    </div>
  );
}

marked.use({gfm: true, breaks: true});

interface StreamingBubbleProps {
  text: string;
  thinkingText: string;
  steps: Step[];
  workers?: WorkerInfo[];
  onShowSteps?: (steps: Step[]) => void;
  artifacts?: ArtifactCard[];
  onOpenArtifact?: (id: string) => void;
  openArtifactId?: string | null;
}

export function StreamingBubble({
  text,
  thinkingText,
  steps,
  workers,
  onShowSteps,
  artifacts,
  onOpenArtifact,
  openArtifactId,
}: StreamingBubbleProps) {
  const latestStep = steps.length > 0 ? steps[steps.length - 1] : null;
  const preview = getStepPreview(latestStep);
  const thinkingRef = useRef<HTMLDivElement | null>(null);
  const html = useMemo(() => marked.parse(text) as string, [text]);

  // Auto-scroll thinking block to bottom as new reasoning tokens arrive.
  useEffect(() => {
    if (thinkingRef.current) {
      thinkingRef.current.scrollTop = thinkingRef.current.scrollHeight;
    }
  }, [thinkingText]);

  const stepsButton = steps.length > 0 && (
    <button {...stylex.props(chipBtn.base)} onClick={() => onShowSteps?.(steps)}>
      <svg
        width="11"
        height="11"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
      </svg>
      {steps.length} step{steps.length !== 1 ? 's' : ''}
    </button>
  );

  return (
    <div {...stylex.props(turn.base)}>
      {workers && workers.length > 0 && <WorkerPanel workers={workers} />}
      {text ? (
        <div {...stylex.props(prose.base)} data-md>
          {/* No `key` here: keying on text.length remounts this node on every
              token, which restarts the fade-in over the whole accumulated
              message and reads as flicker. The fade belongs to the bubble
              mounting once, not to each token. */}
          <span dangerouslySetInnerHTML={{__html: html}} />
          <span {...stylex.props(stream.cursor)} />
        </div>
      ) : (
        <div {...stylex.props(working.root)}>
          {/* Section 1: animated dots + action label */}
          <div {...stylex.props(working.action)}>
            <ThinkingDots />
            <span {...stylex.props(working.label)}>{describeStep(latestStep)}</span>
          </div>
          {/* Section 2: live reasoning text (if available), else step preview */}
          {thinkingText ? (
            <div {...stylex.props(working.thinking)} ref={thinkingRef}>
              {thinkingText}
            </div>
          ) : (
            preview && <div {...stylex.props(working.preview)}>{preview}</div>
          )}
          {/* Section 3: step count */}
          {stepsButton}
        </div>
      )}
      {/* Artifacts land mid-run, so they show as soon as the tool returns
          rather than waiting for the turn to finalize. */}
      {artifacts && artifacts.length > 0 && (
        <MessageArtifacts
          artifacts={artifacts}
          onOpen={onOpenArtifact}
          selectedId={openArtifactId}
        />
      )}
      {/* After text starts streaming, still show step count */}
      {text && stepsButton}
    </div>
  );
}

interface MessageBubbleProps {
  message: Message;
  onShowSteps?: (steps: Step[]) => void;
  /** Artifacts this message produced, rendered as cards beneath it. */
  artifacts?: ArtifactCard[];
  onOpenArtifact?: (id: string) => void;
  openArtifactId?: string | null;
}

function CopyIcon() {
  return (
    <svg
      width="12"
      height="12"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
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
    >
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

export function MessageBubble({
  message,
  onShowSteps,
  artifacts,
  onOpenArtifact,
  openArtifactId,
}: MessageBubbleProps) {
  const [copied, setCopied] = useState(false);
  const [ttsState, setTtsState] = useState<'idle' | 'loading' | 'playing'>('idle');
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      audioRef.current?.pause();
      window.speechSynthesis?.cancel();
    },
    [],
  );

  function copyText(text: string) {
    navigator.clipboard.writeText(text);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  async function readAloud() {
    if (ttsState === 'playing' || ttsState === 'loading') {
      abortRef.current?.abort();
      audioRef.current?.pause();
      audioRef.current = null;
      window.speechSynthesis?.cancel();
      setTtsState('idle');
      return;
    }
    setTtsState('loading');
    abortRef.current = new AbortController();
    try {
      const resp = await fetch('/tts', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text: message.content}),
        signal: abortRef.current.signal,
      });
      if (!resp.ok) throw new Error('tts unavailable');
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audioRef.current = audio;
      audio.onended = () => {
        URL.revokeObjectURL(url);
        setTtsState('idle');
      };
      audio.onerror = () => {
        URL.revokeObjectURL(url);
        setTtsState('idle');
      };
      audio.play();
      setTtsState('playing');
    } catch (e) {
      if ((e as Error).name === 'AbortError') {
        setTtsState('idle');
        return;
      }
      window.speechSynthesis?.speak(new SpeechSynthesisUtterance(message.content));
      setTtsState('idle');
    }
  }

  if (message.role === 'user') {
    const parts = parseMultimodal(message.content);
    const plainText = parts
      ? parts
          .filter((p) => p.type === 'text')
          .map((p) => (p as {type: 'text'; text: string}).text)
          .join('\n')
      : message.content;
    return (
      <div id={messageAnchorId(message.id)} {...stylex.props(turn.base)}>
        <div {...stylex.props(bubble.user)}>
          {parts ? <MultimodalUserContent parts={parts} /> : message.content}
        </div>
        <div {...stylex.props(actions.row, actions.rowUser)}>
          <button
            {...stylex.props(actions.copy, copied && actions.copyDone)}
            onClick={() => copyText(plainText)}
            title={copied ? 'Copied!' : 'Copy'}
            type="button"
          >
            {copied ? <CheckIcon /> : <CopyIcon />}
          </button>
        </div>
      </div>
    );
  }

  const html = marked.parse(message.content) as string;
  const blocked = message.status === 'blocked';

  return (
    <div id={messageAnchorId(message.id)} {...stylex.props(turn.base)}>
      {blocked && (
        <SafetyBanner layer={message.content.startsWith('[OUTPUT REDACTED') ? 'output' : 'input'} />
      )}
      <div
        {...stylex.props(prose.base, blocked && prose.blocked)}
        data-md
        dangerouslySetInnerHTML={{__html: html}}
      />
      {artifacts && artifacts.length > 0 && (
        <MessageArtifacts
          artifacts={artifacts}
          onOpen={onOpenArtifact}
          selectedId={openArtifactId}
        />
      )}
      <div {...stylex.props(actions.row, ttsState !== 'idle' && actions.rowPinned)}>
        <button
          {...stylex.props(actions.copy, copied && actions.copyDone)}
          onClick={() => copyText(message.content)}
          title={copied ? 'Copied!' : 'Copy'}
          type="button"
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
        </button>
        <button
          {...stylex.props(actions.copy, ttsState === 'playing' && actions.copyDone)}
          onClick={readAloud}
          title={
            ttsState === 'loading' ? 'Loading…' : ttsState === 'playing' ? 'Stop' : 'Read aloud'
          }
          type="button"
        >
          {ttsState === 'loading' ? (
            <SpinnerIcon />
          ) : ttsState === 'playing' ? (
            <StopIcon />
          ) : (
            <SpeakerIcon />
          )}
        </button>
        {message.steps.length > 0 && (
          <button {...stylex.props(chipBtn.base)} onClick={() => onShowSteps?.(message.steps)}>
            <svg
              width="11"
              height="11"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
            </svg>
            {message.steps.length} step{message.steps.length !== 1 ? 's' : ''}
          </button>
        )}
        <DebugInfo message={message} />
      </div>
    </div>
  );
}
