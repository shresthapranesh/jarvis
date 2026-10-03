import * as stylex from '@stylexjs/stylex';
import {useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {MaintenanceQuery as TMaintenanceQuery} from '../../__generated__/MaintenanceQuery.graphql';
import {useToast} from '../../lib/toast';
import {commitDownloadVoice} from '../../relay/DownloadVoiceMutation';
import {maintenanceQuery} from '../../relay/MaintenanceQuery';
import {useQueryRetry} from '../QueryBoundary';
import {badge, btn, codeField, page} from '../ui';
import {maint, tools as toolStyles} from './settings.styles';

function bytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

export function MaintenanceTab() {
  const retry = useQueryRetry();
  const [refetch, setRefetch] = useState(0);
  const data = useLazyLoadQuery<TMaintenanceQuery>(
    // `refetch` bumps the fetch key after an action so the status reflects
    // what just happened.
    maintenanceQuery,
    {},
    {fetchPolicy: 'network-only', fetchKey: `${retry}-${refetch}`},
  );

  return (
    <div {...stylex.props(page.section)}>
      <VoiceCard status={data.voiceStatus} onDone={() => setRefetch((n) => n + 1)} />
    </div>
  );
}

function VoiceCard({
  status,
  onDone,
}: {
  status: TMaintenanceQuery['response']['voiceStatus'];
  onDone: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  async function download(force: boolean) {
    setBusy(true);
    try {
      const r = await commitDownloadVoice({force});
      const got = r.files.filter((f) => f.downloaded).length;
      toast.push(
        got ? `Downloaded ${got} file(s) for ${r.voice}.` : 'Voice already present.',
        'success',
      );
      onDone();
    } catch (e) {
      toast.push((e as Error).message || String(e), 'error');
    } finally {
      setBusy(false);
    }
  }

  return (
    <section {...stylex.props(toolStyles.kind)}>
      <h3 {...stylex.props(toolStyles.kindTitle)}>
        Text-to-speech voice
        <span {...stylex.props(toolStyles.kindBlurb)}>
          The Piper voice model behind <code>POST /tts</code>, which 404s until both files are on
          disk. Roughly 60 MB, fetched from the rhasspy/piper-voices repo.
        </span>
      </h3>

      {status.error ? (
        <div {...stylex.props(page.error)}>{status.error}</div>
      ) : (
        <>
          <dl {...stylex.props(maint.stats)}>
            <div>
              <dt>Voice</dt>
              <dd>{status.voice}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>{status.ready ? 'Ready' : 'Not downloaded'}</dd>
            </div>
          </dl>
          <ul {...stylex.props(toolStyles.list)}>
            {status.files.map((f) => (
              <li key={f.name} {...stylex.props(toolStyles.row, !f.exists && toolStyles.rowOff)}>
                <div {...stylex.props(toolStyles.rowMain)}>
                  <div {...stylex.props(toolStyles.rowHead)}>
                    <span {...stylex.props(toolStyles.rowName)}>{f.name}</span>
                    <span {...stylex.props(badge.base)}>
                      {f.exists ? bytes(f.sizeBytes) : 'missing'}
                    </span>
                  </div>
                  <p {...stylex.props(toolStyles.rowDesc)}>{f.path}</p>
                </div>
              </li>
            ))}
          </ul>
          <div {...stylex.props(codeField.actions)}>
            <button
              {...stylex.props(btn.base, btn.primary)}
              disabled={busy || status.ready}
              onClick={() => void download(false)}
            >
              {busy ? 'Downloading…' : 'Download voice'}
            </button>
            <button
              {...stylex.props(btn.base)}
              disabled={busy}
              title="Re-fetch both files even if they exist — the fix for a truncated download."
              onClick={() => void download(true)}
            >
              Re-download
            </button>
          </div>
          <p {...stylex.props(codeField.meta)}>
            Change which voice by setting <code>PIPER_VOICE</code>, then re-download.
          </p>
        </>
      )}
    </section>
  );
}
