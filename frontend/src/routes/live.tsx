import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';
import {marked} from 'marked';
import {memo, useCallback, useEffect, useMemo, useRef, useState} from 'react';

import {ThinkingDots, chipBtn, errorBubble, field, prose, stream} from '../components/ui';
import {useAudioTTS} from '../hooks/useAudioTTS';
import {useLiveSocket, type LiveStatus, type LiveTurn} from '../hooks/useLiveSocket';
import {useModels} from '../hooks/useModels';
import {useSpeechRecognition} from '../hooks/useSpeechRecognition';
import {useWhisperSTT} from '../hooks/useWhisperSTT';
import {describeStep} from '../lib/steps';
import {call, live, orbStyle} from './live.styles';

export const Route = createFileRoute('/live')({component: LivePage});

// ── TurnItem ──────────────────────────────────────────────────────────────────

const TurnItem = memo(function TurnItem({turn}: {turn: LiveTurn}) {
  const html = useMemo(() => marked.parse(turn.text) as string, [turn.text]);
  return (
    <div {...stylex.props(live.turn, turn.role === 'user' ? live.turnUser : live.turnAgent)}>
      {turn.role === 'user' ? (
        <div {...stylex.props(live.userBubble)}>{turn.text}</div>
      ) : (
        <div {...stylex.props(prose.base)} data-md dangerouslySetInnerHTML={{__html: html}} />
      )}
      {turn.role === 'agent' && turn.steps > 0 && (
        <span {...stylex.props(chipBtn.base, live.stepsBadge)}>
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
          {turn.steps} step{turn.steps !== 1 ? 's' : ''}
        </span>
      )}
    </div>
  );
});

// ── LivePage ──────────────────────────────────────────────────────────────────

function LivePage() {
  const {data: catalog} = useModels();
  const [model, setModel] = useState('');

  // Resolve default once the catalog arrives.
  useEffect(() => {
    if (catalog && !model) setModel(catalog.default);
  }, [catalog, model]);
  const [ttsEnabled, setTtsEnabled] = useState(true);
  const [sttMode, setSttMode] = useState<'browser' | 'whisper'>('browser');
  const [callActive, setCallActive] = useState(false);
  const [muted, setMuted] = useState(false);
  const turnsEndRef = useRef<HTMLDivElement | null>(null);

  const {enqueue: enqueueTTS, cancel: cancelTTS} = useAudioTTS();

  const {connected, status, turns, streamText, stepCount, currentStep, error, sendUserMessage} =
    useLiveSocket(model, ttsEnabled ? enqueueTTS : null);

  const handleFinalResult = useCallback(
    (text: string) => {
      sendUserMessage(text);
    },
    [sendUserMessage],
  );

  const browserSTT = useSpeechRecognition(handleFinalResult);
  const whisperSTT = useWhisperSTT(handleFinalResult);
  const activeSTT = sttMode === 'whisper' ? whisperSTT : browserSTT;
  const {supported, listening, interimText, startListening} = activeSTT;
  const cancelListening = (
    'cancelListening' in activeSTT ? activeSTT.cancelListening : activeSTT.stopListening
  ) as () => void;

  // Auto-cycle: restart listening whenever the agent goes idle and call is active
  useEffect(() => {
    if (callActive && !muted && status === 'idle' && !listening) {
      startListening();
    }
  }, [callActive, muted, status, listening, startListening]);

  const parsedStreamHtml = useMemo(() => marked.parse(streamText) as string, [streamText]);

  useEffect(() => {
    if (streamText) turnsEndRef.current?.scrollIntoView({behavior: 'instant'});
  }, [streamText]);
  useEffect(() => {
    turnsEndRef.current?.scrollIntoView({behavior: 'smooth'});
  }, [turns]);

  const handleStartCall = useCallback(() => {
    setMuted(false);
    setCallActive(true);
  }, []);

  const handleEndCall = useCallback(() => {
    cancelListening();
    cancelTTS();
    setCallActive(false);
    setMuted(false);
  }, [cancelListening, cancelTTS]);

  const handleToggleMute = useCallback(() => {
    if (!muted) {
      cancelListening();
      setMuted(true);
    } else {
      setMuted(false);
    }
  }, [muted, cancelListening]);

  const orbState = !callActive ? 'inactive' : muted ? 'muted' : listening ? 'listening' : status;

  const statusLabel = !callActive
    ? connected
      ? 'Ready'
      : 'Connecting…'
    : muted
      ? 'Muted'
      : ((
          {
            idle: 'Listening…',
            listening: 'Listening…',
            thinking: 'Thinking…',
            speaking: 'Responding…',
          } as Record<LiveStatus, string>
        )[status] ?? 'Listening…');

  if (sttMode === 'browser' && !supported) {
    return (
      <div {...stylex.props(live.page)}>
        <div {...stylex.props(live.unsupported)}>
          <svg
            width="32"
            height="32"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
            <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
            <line x1="12" y1="19" x2="12" y2="23" />
            <line x1="8" y1="23" x2="16" y2="23" />
          </svg>
          <h3 {...stylex.props(live.unsupportedTitle)}>Browser not supported</h3>
          <p {...stylex.props(live.unsupportedBody)}>
            Web Speech API requires Chrome or Edge.
            <br />
            Switch to Whisper STT to use any browser.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div {...stylex.props(live.page)}>
      <div {...stylex.props(live.header)}>
        <span {...stylex.props(live.title)}>Live</span>
        <div {...stylex.props(live.headerControls)}>
          <button
            {...stylex.props(live.toggle, sttMode === 'whisper' && live.toggleActive)}
            onClick={() => setSttMode((m) => (m === 'browser' ? 'whisper' : 'browser'))}
            title="Switch STT engine"
          >
            {sttMode === 'whisper' ? 'Whisper' : 'Browser'}
          </button>
          <button
            {...stylex.props(live.toggle, ttsEnabled && live.toggleActive)}
            onClick={() => {
              if (ttsEnabled) cancelTTS();
              setTtsEnabled((v) => !v);
            }}
            title="Toggle text-to-speech"
          >
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
              <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
              <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
            </svg>
            TTS
          </button>
          <select
            {...stylex.props(live.modelInput, field.selectChrome)}
            value={model}
            onChange={(e) => setModel(e.target.value)}
            disabled={!catalog}
            title={catalog ? undefined : 'Loading models…'}
          >
            {!catalog && <option value="">Loading…</option>}
            {catalog?.available.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>
          <div
            {...stylex.props(live.connDot, connected && live.connDotOk)}
            title={connected ? 'Connected' : 'Disconnected'}
          />
        </div>
      </div>

      <div {...stylex.props(live.turns)}>
        {turns.length === 0 && !streamText && (
          <div {...stylex.props(live.empty)}>
            {callActive
              ? 'Speak to start the conversation.'
              : 'Start a call to talk with the agent.'}
          </div>
        )}
        {turns.map((turn: LiveTurn) => (
          <TurnItem key={turn.id} turn={turn} />
        ))}

        {/* Streaming agent response */}
        {streamText && (
          <div {...stylex.props(live.turn, live.turnAgent)}>
            <div {...stylex.props(prose.base)} data-md>
              <span dangerouslySetInnerHTML={{__html: parsedStreamHtml}} />
              <span {...stylex.props(stream.cursor)} />
            </div>
            {stepCount > 0 && (
              <span {...stylex.props(chipBtn.base, live.stepsBadge)}>
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
                {stepCount} step{stepCount !== 1 ? 's' : ''}
              </span>
            )}
          </div>
        )}

        {/* Thinking indicator */}
        {status === 'thinking' && !streamText && (
          <div {...stylex.props(live.turn, live.turnAgent)}>
            <div {...stylex.props(live.thinking)}>
              <ThinkingDots />
              {describeStep(currentStep)}
            </div>
          </div>
        )}

        {error && (
          <div {...stylex.props(live.turn)}>
            <div {...stylex.props(errorBubble.base)}>⚠ {error}</div>
          </div>
        )}

        <div ref={turnsEndRef} />
      </div>

      {interimText && <div {...stylex.props(live.interim)}>"{interimText}…"</div>}

      <div {...stylex.props(live.statusBar)}>{statusLabel}</div>

      <div {...stylex.props(live.controls)}>
        {!callActive ? (
          <button {...stylex.props(call.start)} onClick={handleStartCall} disabled={!connected}>
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07A19.5 19.5 0 0 1 4.69 12 19.79 19.79 0 0 1 1.62 3.4 2 2 0 0 1 3.62 1h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L7.91 8.6a16 16 0 0 0 6 6l.96-.96a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z" />
            </svg>
            Start call
          </button>
        ) : (
          <div {...stylex.props(call.active)}>
            <div {...stylex.props(call.orb, orbStyle(orbState))} data-state={orbState} />
            <button
              {...stylex.props(call.btn, muted && call.btnMuted)}
              onClick={handleToggleMute}
              title={muted ? 'Unmute' : 'Mute'}
            >
              {muted ? (
                <svg
                  width="18"
                  height="18"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <line x1="1" y1="1" x2="23" y2="23" />
                  <path d="M9 9v3a3 3 0 0 0 5.12 2.12M15 9.34V4a3 3 0 0 0-5.94-.6" />
                  <path d="M17 16.95A7 7 0 0 1 5 12v-2m14 0v2a7 7 0 0 1-.11 1.23" />
                  <line x1="12" y1="19" x2="12" y2="23" />
                  <line x1="8" y1="23" x2="16" y2="23" />
                </svg>
              ) : (
                <svg
                  width="18"
                  height="18"
                  viewBox="0 0 24 24"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                >
                  <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z" />
                  <path d="M19 10v2a7 7 0 0 1-14 0v-2" />
                  <line x1="12" y1="19" x2="12" y2="23" />
                  <line x1="8" y1="23" x2="16" y2="23" />
                </svg>
              )}
            </button>
            <button
              {...stylex.props(call.btn, call.btnEnd)}
              onClick={handleEndCall}
              title="End call"
            >
              <svg
                width="18"
                height="18"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M10.68 13.31a16 16 0 0 0 3.41 2.6l1.27-1.27a2 2 0 0 1 2.11-.45 12.84 12.84 0 0 0 2.81.7 2 2 0 0 1 1.72 2v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.42 19.42 0 0 1-3.33-2.67m-2.67-3.34a19.79 19.79 0 0 1-3.07-8.63A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72 12.84 12.84 0 0 0 .7 2.81 2 2 0 0 1-.45 2.11L8.09 9.91" />
                <line x1="23" y1="1" x2="1" y2="23" />
              </svg>
            </button>
          </div>
        )}
      </div>
    </div>
  );
}
