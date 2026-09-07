import {useEffect, useRef, useState} from 'react';

/**
 * Live JPEG frames from the agent's browser over `/ws/browser`.
 *
 * Binary messages are frames, text messages are control. Frames are decoded to
 * an ImageBitmap and handed over as they land — the caller paints them to a
 * canvas, which avoids the object-URL churn an <img> per frame would cause at
 * ~15fps (each one needs revoking, and missing one leaks the blob).
 *
 * The socket is only opened while `enabled`, because the server starts and
 * stops the screencast on subscriber count: a closed panel must cost no
 * encoding in the browser.
 */

export type BrowserStreamStatus = 'idle' | 'connecting' | 'live' | 'unavailable';

export interface BrowserStreamMeta {
  width: number;
  height: number;
  url: string;
}

export interface BrowserStreamState {
  status: BrowserStreamStatus;
  meta: BrowserStreamMeta | null;
  reason: string;
  /** Latest decoded frame. Owned by the hook — do not close it. */
  frame: ImageBitmap | null;
}

function socketUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/ws/browser`;
}

export function useBrowserStream(enabled: boolean): BrowserStreamState {
  const [status, setStatus] = useState<BrowserStreamStatus>('idle');
  const [meta, setMeta] = useState<BrowserStreamMeta | null>(null);
  const [reason, setReason] = useState('');
  const [frame, setFrame] = useState<ImageBitmap | null>(null);
  // The frame currently on screen, so the replacing effect can close it.
  // ImageBitmaps hold GPU-side memory that GC will not reclaim promptly.
  const current = useRef<ImageBitmap | null>(null);

  useEffect(() => {
    if (!enabled) {
      setStatus('idle');
      setMeta(null);
      setFrame(null);
      current.current?.close();
      current.current = null;
      return;
    }

    let closed = false;
    setStatus('connecting');
    const ws = new WebSocket(socketUrl());
    ws.binaryType = 'arraybuffer';

    ws.onmessage = async (ev) => {
      if (typeof ev.data === 'string') {
        let msg: {type?: string; state?: string; reason?: string} & Partial<BrowserStreamMeta>;
        try {
          msg = JSON.parse(ev.data);
        } catch {
          return;
        }
        if (msg.type === 'status') {
          if (msg.state === 'live') setStatus('live');
          if (msg.state === 'unavailable') {
            setStatus('unavailable');
            setReason(msg.reason ?? '');
          }
        } else if (msg.type === 'meta') {
          setMeta({width: msg.width ?? 0, height: msg.height ?? 0, url: msg.url ?? ''});
        }
        return;
      }
      // A frame can still be decoding when the panel closes; dropping it then
      // would leak the bitmap, so close it instead of setting state.
      const bitmap = await createImageBitmap(new Blob([ev.data as ArrayBuffer], {type: 'image/jpeg'}));
      if (closed) {
        bitmap.close();
        return;
      }
      current.current?.close();
      current.current = bitmap;
      setFrame(bitmap);
    };

    ws.onerror = () => {
      if (!closed) setStatus('unavailable');
    };
    ws.onclose = () => {
      if (!closed) setStatus((s) => (s === 'unavailable' ? s : 'idle'));
    };

    return () => {
      closed = true;
      current.current?.close();
      current.current = null;
      ws.close();
    };
  }, [enabled]);

  return {status, meta, reason, frame};
}
