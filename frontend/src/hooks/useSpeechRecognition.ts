import {useCallback, useEffectEvent, useRef, useState} from 'react';

const SR = (window as any).SpeechRecognition ?? (window as any).webkitSpeechRecognition;

export function useSpeechRecognition(onFinalResult: (text: string) => void) {
  const recogRef = useRef<any>(null);
  const [supported] = useState(() => !!SR);
  const [listening, setListening] = useState(false);
  const [interimText, setInterimText] = useState('');

  // useEffectEvent: stable reference that always reads the latest onFinalResult
  const onResult = useEffectEvent(onFinalResult);

  const startListening = useCallback(() => {
    if (!SR) return;
    const recog = new SR();
    recog.continuous = false;
    recog.interimResults = true;
    recog.lang = 'en-US';

    recog.onresult = (e: any) => {
      let interim = '';
      let final = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        const t = e.results[i][0].transcript;
        if (e.results[i].isFinal) final += t;
        else interim += t;
      }
      setInterimText(interim);
      if (final.trim()) {
        setInterimText('');
        setListening(false);
        onResult(final.trim());
      }
    };

    recog.onerror = () => {
      setListening(false);
      setInterimText('');
    };
    recog.onend = () => {
      setListening(false);
      setInterimText('');
    };

    recogRef.current = recog;
    recog.start();
    setListening(true);
  }, [onResult]);

  const stopListening = useCallback(() => {
    recogRef.current?.stop();
    setListening(false);
    setInterimText('');
  }, []);

  return {supported, listening, interimText, startListening, stopListening};
}
