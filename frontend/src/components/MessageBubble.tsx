import {marked} from 'marked';
import {useEffect, useRef, useState} from 'react';

function SpeakerIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
      <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="6" y="6" width="12" height="12" rx="2" />
    </svg>
  );
}

function SpinnerIcon() {
  return (
    <svg className="mic-spinner" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 12a9 9 0 1 1-6.219-8.56" />
    </svg>
  );
}

import {describeStep, getStepPreview} from '../lib/steps';
import type {SafetyBlock} from '../hooks/useStream';
import type {Message, Step} from '../lib/types';

function SafetyBanner({block}: {block: SafetyBlock}) {
  const layerLabel = block.layer === 'input' ? 'Input blocked' : 'Output redacted';
  const sev = block.severity ? ` · ${block.severity}` : '';
  return (
    <div className={`safety-banner safety-banner--${block.layer}`}>
      <span className="safety-banner__label">⚠ {layerLabel}{sev}</span>
      {block.reason && <span className="safety-banner__reason">{block.reason}</span>}
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

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function MultimodalUserContent({parts}: {parts: ContentPart[]}) {
  return (
    <div className="multimodal-user">
      {parts.map((part, i) => {
        if (part.type === 'text') {
          return part.text ? (
            <p key={i} className="multimodal-text">
              {part.text}
            </p>
          ) : null;
        }
        if (part.type === 'image') {
          return (
            <div key={i} className="multimodal-file-badge">
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
              <span className="multimodal-size">{formatBytes(part.size)}</span>
            </div>
          );
        }
        if (part.type === 'audio') {
          return (
            <div key={i} className="multimodal-file-badge">
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
              <span className="multimodal-size">{formatBytes(part.size)}</span>
            </div>
          );
        }
        if (part.type === 'video') {
          return (
            <div key={i} className="multimodal-file-badge">
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
              <span className="multimodal-size">{formatBytes(part.size)}</span>
            </div>
          );
        }
        if (part.type === 'document') {
          return (
            <div key={i} className="multimodal-file-badge">
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
              <span className="multimodal-size">{formatBytes(part.size)}</span>
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
  safetyBlock?: SafetyBlock | null;
  onShowSteps?: (steps: Step[]) => void;
}

export function StreamingBubble({text, thinkingText, steps, safetyBlock, onShowSteps}: StreamingBubbleProps) {
  const latestStep = steps.length > 0 ? steps[steps.length - 1] : null;
  const preview = getStepPreview(latestStep);
  const thinkingRef = useRef<HTMLDivElement | null>(null);

  // Auto-scroll thinking block to bottom as new reasoning tokens arrive.
  useEffect(() => {
    if (thinkingRef.current) {
      thinkingRef.current.scrollTop = thinkingRef.current.scrollHeight;
    }
  }, [thinkingText]);

  const stepsButton = steps.length > 0 && (
    <button className="activity-btn" onClick={() => onShowSteps?.(steps)}>
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
    <div className="turn">
      {safetyBlock && <SafetyBanner block={safetyBlock} />}
      {text ? (
        <div className={`agent-bubble streaming${safetyBlock ? ' agent-bubble--blocked' : ''}`}>
          <span dangerouslySetInnerHTML={{__html: marked.parse(text) as string}} />
          {!safetyBlock && <span className="cursor" />}
        </div>
      ) : (
        <div className="working-widget">
          {/* Section 1: animated dots + action label */}
          <div className="working-action">
            <div className="thinking-dots">
              <span />
              <span />
              <span />
            </div>
            <span className="working-label">{describeStep(latestStep)}</span>
          </div>
          {/* Section 2: live reasoning text (if available), else step preview */}
          {thinkingText ? (
            <div className="thinking-stream" ref={thinkingRef}>
              {thinkingText}
            </div>
          ) : (
            preview && <div className="working-preview">{preview}</div>
          )}
          {/* Section 3: step count */}
          {stepsButton}
        </div>
      )}
      {/* After text starts streaming, still show step count */}
      {text && stepsButton}
    </div>
  );
}

interface MessageBubbleProps {
  message: Message;
  onShowSteps?: (steps: Step[]) => void;
}

function CopyIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="9" y="9" width="13" height="13" rx="2" ry="2" />
      <path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="20 6 9 17 4 12" />
    </svg>
  );
}

export function MessageBubble({message, onShowSteps}: MessageBubbleProps) {
  const [copied, setCopied] = useState(false);
  const [ttsState, setTtsState] = useState<'idle' | 'loading' | 'playing'>('idle');
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => () => {
    abortRef.current?.abort();
    audioRef.current?.pause();
    window.speechSynthesis?.cancel();
  }, []);

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
      audio.onended = () => { URL.revokeObjectURL(url); setTtsState('idle'); };
      audio.onerror = () => { URL.revokeObjectURL(url); setTtsState('idle'); };
      audio.play();
      setTtsState('playing');
    } catch (e) {
      if ((e as Error).name === 'AbortError') { setTtsState('idle'); return; }
      window.speechSynthesis?.speak(new SpeechSynthesisUtterance(message.content));
      setTtsState('idle');
    }
  }

  if (message.role === 'user') {
    const parts = parseMultimodal(message.content);
    const plainText = parts
      ? parts.filter((p) => p.type === 'text').map((p) => (p as {type: 'text'; text: string}).text).join('\n')
      : message.content;
    return (
      <div className="turn">
        <div className="user-bubble">
          {parts ? <MultimodalUserContent parts={parts} /> : message.content}
        </div>
        <div className="turn-actions turn-actions--user">
          <button
            className={`copy-btn${copied ? ' copy-btn--copied' : ''}`}
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
  // Persisted blocks have no streaming-side reason payload; the bubble
  // content itself carries the rejection/redaction text. Infer the layer
  // from the content prefix so users still see which gate fired.
  const persistedBlock: SafetyBlock | null = blocked
    ? {layer: message.content.startsWith('[OUTPUT REDACTED') ? 'output' : 'input'}
    : null;

  return (
    <div className="turn">
      {persistedBlock && <SafetyBanner block={persistedBlock} />}
      <div
        className={`agent-bubble${blocked ? ' agent-bubble--blocked' : ''}`}
        dangerouslySetInnerHTML={{__html: html}}
      />
      <div className={`turn-actions${ttsState !== 'idle' ? ' turn-actions--tts-active' : ''}`}>
        <button
          className={`copy-btn${copied ? ' copy-btn--copied' : ''}`}
          onClick={() => copyText(message.content)}
          title={copied ? 'Copied!' : 'Copy'}
          type="button"
        >
          {copied ? <CheckIcon /> : <CopyIcon />}
        </button>
        <button
          className={`copy-btn${ttsState === 'playing' ? ' copy-btn--copied' : ''}`}
          onClick={readAloud}
          title={ttsState === 'loading' ? 'Loading…' : ttsState === 'playing' ? 'Stop' : 'Read aloud'}
          type="button"
        >
          {ttsState === 'loading' ? <SpinnerIcon /> : ttsState === 'playing' ? <StopIcon /> : <SpeakerIcon />}
        </button>
        {message.steps.length > 0 && (
          <button className="activity-btn" onClick={() => onShowSteps?.(message.steps)}>
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
      </div>
    </div>
  );
}
