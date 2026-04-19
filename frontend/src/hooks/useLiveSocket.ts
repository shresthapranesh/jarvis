import {useCallback, useEffect, useEffectEvent, useRef, useState} from 'react';

import type {Step} from '../lib/types';

export type LiveStatus = 'idle' | 'listening' | 'thinking' | 'speaking';

export interface LiveTurn {
  id: string;
  role: 'user' | 'agent';
  text: string;
  steps: number;
}

const SENTENCE_RE = /[^.!?]*[.!?]+(?=\s|$)/g;

export function useLiveSocket(
  model: string,
  // Null when TTS is disabled — passed directly, no ref needed on the caller side.
  onEnqueueTTS: ((sentence: string) => void) | null,
) {
  const wsRef = useRef<WebSocket | null>(null);
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<LiveStatus>('idle');
  const [turns, setTurns] = useState<LiveTurn[]>([]);
  const [streamText, setStreamText] = useState('');
  const [stepCount, setStepCount] = useState(0);
  // Latest step seen in the current agent turn; null while idle or after
  // `done`/`error`. Drives the live activity label (describeStep) so the
  // user sees "Calling web_search…" / "Running researcher…" instead of a
  // static "Working…".
  const [currentStep, setCurrentStep] = useState<Step | null>(null);
  const [error, setError] = useState<string | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const modelRef = useRef(model);
  modelRef.current = model;

  // TTS accumulation — owned here so sentence detection happens at the moment
  // tokens arrive, not after React re-renders.
  const ttsBufRef = useRef(''); // full accumulated stream text
  const ttsCursorRef = useRef(0); // how far into ttsBufRef we've already enqueued

  // useEffectEvent: stable reference that always reads the latest onEnqueueTTS,
  // so connect()'s empty dep array doesn't need updating when TTS is toggled.
  const enqueueTTSEvent = useEffectEvent((sentence: string) => {
    onEnqueueTTS?.(sentence);
  });

  const connect = useCallback(() => {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    const ws = new WebSocket(
      `${proto}://${location.host}/ws/live?model=${encodeURIComponent(modelRef.current)}`,
    );
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      setError(null);
    };

    ws.onmessage = (e) => {
      const msg = JSON.parse(e.data) as Record<string, string>;
      if (msg.type === 'token') {
        setStreamText((t) => t + msg.text);
        setStatus('speaking');
        // Sentence detection — runs at arrival time, no useEffect needed
        ttsBufRef.current += msg.text;
        const unspoken = ttsBufRef.current.slice(ttsCursorRef.current);
        SENTENCE_RE.lastIndex = 0;
        let match: RegExpExecArray | null;
        let lastEnd = 0;
        while ((match = SENTENCE_RE.exec(unspoken)) !== null) {
          const sentence = match[0].trim();
          if (sentence) enqueueTTSEvent(sentence);
          lastEnd = SENTENCE_RE.lastIndex;
        }
        ttsCursorRef.current += lastEnd;
      } else if (msg.type === 'step') {
        setStepCount((c) => c + 1);
        setCurrentStep({
          id: crypto.randomUUID(),
          node: msg.node as string,
          source: msg.source as string,
          subagent: (msg.subagent as string | null) ?? null,
          data: (msg.data as string | null) ?? null,
          seq: 0,
          created_at: new Date().toISOString(),
        });
      } else if (msg.type === 'done') {
        // Enqueue any trailing text that didn't end with punctuation
        const remainder = ttsBufRef.current.slice(ttsCursorRef.current).trim();
        if (remainder) enqueueTTSEvent(remainder);
        const finalText = msg.text || ttsBufRef.current;
        ttsBufRef.current = '';
        ttsCursorRef.current = 0;

        setTurns((prev) => [
          ...prev,
          {
            id: crypto.randomUUID(),
            role: 'agent',
            text: finalText,
            steps: 0, // stepCount is stale in this closure; steps badge is shown live
          },
        ]);
        setStreamText('');
        setStepCount(0);
        setCurrentStep(null);
        setStatus('idle');
      } else if (msg.type === 'status') {
        if (msg.state === 'thinking') setStatus('thinking');
        if (msg.state === 'idle') setStatus('idle');
      } else if (msg.type === 'error') {
        setError(msg.error);
        setStatus('idle');
        setStreamText('');
        setStepCount(0);
        setCurrentStep(null);
        ttsBufRef.current = '';
        ttsCursorRef.current = 0;
      }
    };

    ws.onclose = () => {
      setConnected(false);
      setStatus('idle');
      reconnectTimer.current = setTimeout(connect, 2000);
    };

    ws.onerror = () => ws.close();
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    connect();
    return () => {
      if (reconnectTimer.current) clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [connect]);

  const sendUserMessage = useCallback((text: string) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      setTurns((prev) => [...prev, {id: crypto.randomUUID(), role: 'user', text, steps: 0}]);
      setError(null);
      wsRef.current.send(JSON.stringify({type: 'user_message', text}));
    }
  }, []);

  return {connected, status, turns, streamText, stepCount, currentStep, error, sendUserMessage};
}
