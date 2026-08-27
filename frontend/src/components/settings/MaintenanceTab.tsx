import * as stylex from '@stylexjs/stylex';
import {useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {MaintenanceQuery as TMaintenanceQuery} from '../../__generated__/MaintenanceQuery.graphql';
import {useToast} from '../../lib/toast';
import {commitDownloadVoice} from '../../relay/DownloadVoiceMutation';
import {maintenanceQuery} from '../../relay/MaintenanceQuery';
import {commitPruneCheckpoints} from '../../relay/PruneCheckpointsMutation';
import {ConfirmDialog} from '../ConfirmDialog';
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
    // Two independent maintenance surfaces in one round trip; `refetch` bumps
    // the fetch key after an action so the numbers reflect what just happened.
    maintenanceQuery,
    {},
    {fetchPolicy: 'network-only', fetchKey: `${retry}-${refetch}`},
  );

  return (
    <div {...stylex.props(page.section)}>
      <CheckpointCard stats={data.checkpointStats} onDone={() => setRefetch((n) => n + 1)} />
      <VoiceCard status={data.voiceStatus} onDone={() => setRefetch((n) => n + 1)} />
    </div>
  );
}

function CheckpointCard({
  stats,
  onDone,
}: {
  stats: TMaintenanceQuery['response']['checkpointStats'];
  onDone: () => void;
}) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [confirm, setConfirm] = useState(false);
  const [result, setResult] = useState<string | null>(null);
  const prunable = stats.prunableRoot + stats.prunableSubgraph;

  async function run(dryRun: boolean) {
    setBusy(true);
    try {
      const r = await commitPruneCheckpoints({dryRun});
      const removed = r.rootPruned + r.subgraphPruned;
      setResult(
        `${dryRun ? 'Would remove' : 'Removed'} ${removed} checkpoint(s) ` +
          `(${r.rootPruned} root, ${r.subgraphPruned} subgraph), ${bytes(r.bytesFreed)}. ${r.note}`,
      );
      if (!dryRun) onDone();
    } catch (e) {
      toast.push((e as Error).message || String(e), 'error');
    } finally {
      setBusy(false);
      setConfirm(false);
    }
  }

  return (
    <section {...stylex.props(toolStyles.kind)}>
      <h3 {...stylex.props(toolStyles.kindTitle)}>
        Checkpoint retention
        <span {...stylex.props(toolStyles.kindBlurb)}>
          LangGraph re-serializes the whole graph state on every super-step and never reclaims the
          superseded snapshots, so <code>checkpoints.db</code> grows with run length. This is the
          same online sweep the hourly job runs.
        </span>
      </h3>

      {!stats.exists ? (
        <div {...stylex.props(page.empty)}>
          No checkpoint database at <code>{stats.dbPath}</code> yet.
        </div>
      ) : (
        <>
          <dl {...stylex.props(maint.stats)}>
            <div>
              <dt>Database</dt>
              <dd>{bytes(stats.sizeBytes)}</dd>
            </div>
            <div>
              <dt>Threads</dt>
              <dd>{stats.threads}</dd>
            </div>
            <div>
              <dt>Checkpoints</dt>
              <dd>
                {stats.checkpoints}
                <span {...stylex.props(maint.sub)}>{stats.subgraphCheckpoints} subgraph</span>
              </dd>
            </div>
            <div>
              <dt>Prunable now</dt>
              <dd>
                {prunable}
                <span {...stylex.props(maint.sub)}>
                  {bytes(stats.reclaimableBytes)} reclaimable
                </span>
              </dd>
            </div>
          </dl>
          <p {...stylex.props(toolStyles.rowDesc)}>
            {/* A low prunable count against a large total is the two guards working, not a
                bug — worth saying, or the number reads as a disappointment. */}
            Checkpoints younger than an hour, and every thread with a run in flight (
            {stats.activeThreads} right now), are skipped — a row that survives this sweep is picked
            up by the next one. <code>{stats.dbPath}</code>
          </p>
          <div {...stylex.props(codeField.actions)}>
            <button {...stylex.props(btn.base)} disabled={busy} onClick={() => void run(true)}>
              Dry run
            </button>
            <button
              {...stylex.props(btn.base, btn.primary)}
              disabled={busy || prunable === 0}
              onClick={() => setConfirm(true)}
            >
              Prune {prunable > 0 ? prunable : ''}
            </button>
          </div>
          {result && <p {...stylex.props(maint.result)}>{result}</p>}
        </>
      )}

      <ConfirmDialog
        open={confirm}
        title="Prune superseded checkpoints?"
        message="Deletes checkpoint rows that no resume path reads. Safe to run against a live server — in-flight threads and anything under an hour old are skipped."
        confirmLabel="Prune"
        danger
        onConfirm={() => void run(false)}
        onCancel={() => setConfirm(false)}
      />
    </section>
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
