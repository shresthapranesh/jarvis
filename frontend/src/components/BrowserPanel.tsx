import * as stylex from '@stylexjs/stylex';
import {useEffect, useRef} from 'react';

import {useBrowserStream} from '../hooks/useBrowserStream';
import {GlobeIcon} from './icons';
import {closeBtn, ResizeHandle, useResizableWidth} from './ui';
import {browserPanel as s} from './BrowserPanel.styles';

/**
 * Live view of the browser `read(url, browser=True)` drives.
 *
 * Frames are painted to a canvas rather than swapped into an <img>: at ~15fps
 * an image element per frame means an object URL per frame, each needing a
 * revoke, and one missed revoke leaks for the life of the tab. The canvas is
 * sized to the frame and scaled by CSS, so resizing the panel costs no
 * re-decode and the aspect ratio is the browser's, not the panel's.
 *
 * View-only. The socket carries a client→server channel already, so click and
 * key forwarding is a message type away, but a challenge is still better
 * solved in the real window — some widgets ignore synthesized input.
 */
export function BrowserPanel({onClose}: {onClose: () => void}) {
  const [width, setWidth] = useResizableWidth('jarvis.browserPanelWidth', 520);
  const {status, meta, reason, frame} = useBrowserStream(true);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !frame) return;
    if (canvas.width !== frame.width || canvas.height !== frame.height) {
      canvas.width = frame.width;
      canvas.height = frame.height;
    }
    canvas.getContext('2d')?.drawImage(frame, 0, 0);
  }, [frame]);

  return (
    <aside {...stylex.props(s.root)} style={{width}} aria-label="Browser view">
      <ResizeHandle width={width} onResize={setWidth} label="Resize browser panel" />
      <header {...stylex.props(s.header)}>
        <div {...stylex.props(s.heading)}>
          <GlobeIcon size={14} />
          <span {...stylex.props(s.title)}>Browser</span>
          {status === 'live' && <span {...stylex.props(s.liveDot)} aria-label="live" />}
        </div>
        <button {...stylex.props(closeBtn.base)} onClick={onClose} title="Close">
          ×
        </button>
      </header>

      {meta?.url && (
        <div {...stylex.props(s.urlBar)} title={meta.url}>
          {meta.url}
        </div>
      )}

      <div {...stylex.props(s.stage)}>
        {status === 'unavailable' ? (
          <p {...stylex.props(s.notice)}>
            No browser to show.
            {reason ? <span {...stylex.props(s.reason)}>{reason}</span> : null}
          </p>
        ) : !frame ? (
          <p {...stylex.props(s.notice)}>
            {status === 'connecting' ? 'Connecting…' : 'Waiting for the first frame…'}
          </p>
        ) : null}
        {/* Kept mounted under the notice: the first frame should appear in
            place rather than remounting the canvas and flashing. */}
        <canvas ref={canvasRef} {...stylex.props(s.canvas, !frame && s.canvasHidden)} />
      </div>

      <footer {...stylex.props(s.footer)}>
        View only — click in the browser window itself to interact.
      </footer>
    </aside>
  );
}
