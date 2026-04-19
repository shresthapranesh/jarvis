import {useCallback, useEffectEvent, useRef, useState} from 'react';

export function useWhisperSTT(onFinalResult: (text: string) => void) {
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const silenceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const speechStartedRef = useRef(false);
  const cancelRef = useRef(false);
  const [listening, setListening] = useState(false);
  const [interimText, setInterimText] = useState('');

  const onResult = useEffectEvent(onFinalResult);

  const stopAndTranscribe = useCallback(() => {
    const mr = mediaRecorderRef.current;
    if (!mr || mr.state === 'inactive') return;
    mr.stop();
    setListening(false);
    setInterimText('Transcribing…');
    if (silenceTimerRef.current) {
      clearTimeout(silenceTimerRef.current);
      silenceTimerRef.current = null;
    }
  }, []);

  const cancelListening = useCallback(() => {
    cancelRef.current = true;
    const mr = mediaRecorderRef.current;
    if (mr && mr.state !== 'inactive') mr.stop();
    setListening(false);
    setInterimText('');
    if (silenceTimerRef.current) {
      clearTimeout(silenceTimerRef.current);
      silenceTimerRef.current = null;
    }
  }, []);

  const startListening = useCallback(async () => {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    const mr = new MediaRecorder(stream, {mimeType: 'audio/webm'});
    mediaRecorderRef.current = mr;
    chunksRef.current = [];
    speechStartedRef.current = false;
    cancelRef.current = false;

    // VAD via Web Audio AnalyserNode
    const ctx = new AudioContext();
    const src = ctx.createMediaStreamSource(stream);
    const analyser = ctx.createAnalyser();
    analyser.fftSize = 256;
    src.connect(analyser);
    const buf = new Uint8Array(analyser.frequencyBinCount);
    const SILENCE_THRESHOLD = 10;
    const SILENCE_MS = 600;
    const vadInterval = setInterval(() => {
      analyser.getByteFrequencyData(buf);
      const avg = buf.reduce((a, b) => a + b, 0) / buf.length;
      if (avg > SILENCE_THRESHOLD) {
        speechStartedRef.current = true;
        if (silenceTimerRef.current) {
          clearTimeout(silenceTimerRef.current);
          silenceTimerRef.current = null;
        }
      } else if (speechStartedRef.current && !silenceTimerRef.current) {
        silenceTimerRef.current = setTimeout(stopAndTranscribe, SILENCE_MS);
      }
    }, 100);
    const maxTimer = setTimeout(stopAndTranscribe, 30_000);

    mr.ondataavailable = (e) => {
      if (e.data.size > 0) chunksRef.current.push(e.data);
    };
    mr.onstop = async () => {
      clearInterval(vadInterval);
      clearTimeout(maxTimer);
      stream.getTracks().forEach((t) => t.stop());
      ctx.close();
      if (cancelRef.current) {
        cancelRef.current = false;
        setInterimText('');
        return;
      }
      const blob = new Blob(chunksRef.current, {type: 'audio/webm'});
      const form = new FormData();
      form.append('audio', blob, 'audio.webm');
      try {
        const resp = await fetch('/transcribe', {method: 'POST', body: form});
        const {text} = await resp.json();
        if (text) onResult(text);
      } catch {
        /* transcription failed — ignore */
      }
      setInterimText('');
    };

    mr.start(100);
    setListening(true);
    setInterimText('Recording…');
  }, [stopAndTranscribe]);

  return {
    supported: true,
    listening,
    interimText,
    startListening,
    stopListening: stopAndTranscribe,
    cancelListening,
  };
}
