import {useCallback, useRef} from 'react';

export function useAudioTTS() {
  const queueRef = useRef<string[]>([]);
  const currentAudioRef = useRef<HTMLAudioElement | null>(null);
  const playingRef = useRef(false);

  const playNext = useCallback(() => {
    const url = queueRef.current.shift();
    if (!url) {
      playingRef.current = false;
      return;
    }
    playingRef.current = true;
    const audio = new Audio(url);
    currentAudioRef.current = audio;
    audio.onended = () => {
      URL.revokeObjectURL(url);
      playNext();
    };
    audio.onerror = () => {
      URL.revokeObjectURL(url);
      playNext();
    };
    audio.play().catch(() => {
      URL.revokeObjectURL(url);
      playNext();
    });
  }, []);

  const enqueue = useCallback(
    async (text: string) => {
      try {
        const resp = await fetch('/tts', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({text}),
        });
        if (!resp.ok) throw new Error('tts unavailable');
        const blob = await resp.blob();
        const url = URL.createObjectURL(blob);
        queueRef.current.push(url);
        if (!playingRef.current) playNext();
      } catch {
        window.speechSynthesis?.speak(new SpeechSynthesisUtterance(text));
      }
    },
    [playNext],
  );

  const cancel = useCallback(() => {
    currentAudioRef.current?.pause();
    currentAudioRef.current = null;
    for (const url of queueRef.current) URL.revokeObjectURL(url);
    queueRef.current = [];
    playingRef.current = false;
    window.speechSynthesis?.cancel();
  }, []);

  return {enqueue, cancel};
}
