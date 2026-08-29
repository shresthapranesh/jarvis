import * as stylex from '@stylexjs/stylex';
import {useCallback, useMemo, useState} from 'react';
import {createPortal} from 'react-dom';

import {useAsyncAction} from '../../hooks/useAsyncAction';
import {useToast} from '../../lib/toast';
import type {DiscoveredModelDraft} from '../../relay/AddDiscoveredModelsMutation';
import type {SyncReport} from '../../relay/ModelSyncQuery';
import {fetchModelSync} from '../../relay/ModelSyncQuery';
import {formModal} from '../FormModal';
import {AlertIcon, CheckIcon, PlusIcon, SearchIcon, SyncIcon} from '../icons';
import {badge, btn, field, modal, page} from '../ui';
import {models, settings, settingsBadge, sync} from './settings.styles';

/**
 * `model sync` in the UI: diff the catalog against what each provider actually
 * offers, then choose what to do about it.
 *
 * The split mirrors the CLI's, and for the same reason — discovery is a lint,
 * not a source of truth. Reading is one action, writing is another: nothing is
 * registered until a specific model is selected and added. `gone` and
 * `unreachable` are reported only; a built-in's entry is compiled in, so there
 * is nothing to click.
 */

const fmt = (n: number | null | undefined) => (n ? n.toLocaleString() : '—');

export function ModelSyncModal({
  providers,
  onClose,
  onCatalogChanged,
}: {
  providers: readonly string[];
  onClose: () => void;
  onCatalogChanged: () => unknown;
}) {
  const toast = useToast();
  const [provider, setProvider] = useState<string>('');
  const [probe, setProbe] = useState(false);
  const [reports, setReports] = useState<readonly SyncReport[] | null>(null);
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [filter, setFilter] = useState('');
  const [showNonChat, setShowNonChat] = useState(false);

  const syncAct = useAsyncAction(
    async (target: string, doProbe: boolean) => {
      const res = await fetchModelSync(target || null, doProbe);
      setReports(res);
      setSelected(new Set());
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  const addAct = useAsyncAction(
    async (models: readonly DiscoveredModelDraft[]) => {
      const {commitAddDiscoveredModels} = await import('../../relay/AddDiscoveredModelsMutation');
      await commitAddDiscoveredModels(models);
      toast.push(
        models.length === 1 ? `Added ${models[0].id}` : `Added ${models.length} models`,
        'success',
      );
      await onCatalogChanged();
      // The catalog moved, so the report is now stale about what's "new".
      await syncAct.run(provider, false);
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  const toggle = useCallback((id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const newRows = useMemo(() => {
    const q = filter.toLowerCase().trim();
    return (reports ?? []).flatMap((r) =>
      r.newModels
        .filter((m) => (showNonChat ? true : m.likelyChat))
        .filter(
          (m) => !q || m.modelId.toLowerCase().includes(q) || m.label.toLowerCase().includes(q),
        ),
    );
  }, [reports, filter, showNonChat]);

  const hiddenNonChat = useMemo(
    () => (reports ?? []).reduce((n, r) => n + r.newModels.filter((m) => !m.likelyChat).length, 0),
    [reports],
  );

  const selectedDrafts = useMemo(
    () =>
      newRows
        .filter((m) => selected.has(m.modelId))
        .map((m) => ({
          id: m.modelId,
          label: m.label,
          provider: m.provider,
          contextWindow: m.contextWindow,
        })),
    [newRows, selected],
  );

  const busy = syncAct.pending || addAct.pending;

  return createPortal(
    <div {...stylex.props(modal.backdrop)} onClick={onClose}>
      {/* Not a `FormModal`: the body here is a scrolling report rather than a
          field stack. It borrows that component's shell styles so the two
          dialogs still read as the same thing. */}
      <div
        {...stylex.props(modal.panel, formModal.panel, formModal.panelWide, sync.modal)}
        onClick={(e) => e.stopPropagation()}
        role="dialog"
        aria-modal="true"
        aria-labelledby="model-sync-title"
      >
        <header>
          <h2 {...stylex.props(modal.title)} id="model-sync-title">
            Sync catalog with providers
          </h2>
          <p {...stylex.props(modal.subtitle)}>
            Compares the catalog against what each provider currently offers. Read-only until you
            add something — a listing reports what a provider publishes, not what your credentials
            can actually call.
          </p>
        </header>

        <div {...stylex.props(sync.controls)}>
          <select
            {...stylex.props(field.select, sync.providerSelect, field.selectChrome)}
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
            disabled={busy}
          >
            <option value="">All discoverable providers</option>
            {providers.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
          <label
            {...stylex.props(sync.check)}
            title="Issues one real one-token call per catalog model"
          >
            <input
              type="checkbox"
              checked={probe}
              onChange={(e) => setProbe(e.target.checked)}
              disabled={busy}
            />
            Probe entitlement
          </label>
          <button
            {...stylex.props(btn.base, btn.primary)}
            disabled={busy}
            onClick={() => void syncAct.run(provider, probe)}
          >
            <SyncIcon size={14} /> {syncAct.pending ? 'Syncing…' : 'Run sync'}
          </button>
        </div>
        {probe && (
          <p {...stylex.props(field.hint, sync.note)}>
            Probing costs one request per catalog model and can take a while — it is the only thing
            that separates “published” from “callable by this account”.
          </p>
        )}

        <div {...stylex.props(formModal.fields, sync.body)}>
          {reports === null ? (
            <div {...stylex.props(page.empty)}>
              {syncAct.pending ? 'Asking each provider…' : 'Run a sync to see catalog drift.'}
            </div>
          ) : (
            <>
              {reports.map((r) => (
                <ReportBlock key={r.provider} report={r} />
              ))}

              {newRows.length > 0 && (
                <section {...stylex.props(sync.section)}>
                  <h3 {...stylex.props(sync.heading)}>
                    New models
                    <span {...stylex.props(page.count)}>{newRows.length}</span>
                    <span {...stylex.props(settings.sectionActions)}>
                      <button
                        {...stylex.props(btn.base, btn.small)}
                        onClick={() => setSelected(new Set(newRows.map((m) => m.modelId)))}
                      >
                        Select all
                      </button>
                      <button
                        {...stylex.props(btn.base, btn.small)}
                        onClick={() => setSelected(new Set())}
                      >
                        Clear
                      </button>
                    </span>
                  </h3>
                  <div {...stylex.props(sync.filter)}>
                    <div {...stylex.props(settings.search)}>
                      <SearchIcon size={14} />
                      <input
                        {...stylex.props(settings.searchInput)}
                        placeholder="Filter new models…"
                        value={filter}
                        onChange={(e) => setFilter(e.target.value)}
                      />
                    </div>
                    {hiddenNonChat > 0 && (
                      <label {...stylex.props(sync.check)}>
                        <input
                          type="checkbox"
                          checked={showNonChat}
                          onChange={(e) => setShowNonChat(e.target.checked)}
                        />
                        Show {hiddenNonChat} non-chat
                      </label>
                    )}
                  </div>
                  <ul {...stylex.props(sync.list)}>
                    {newRows.map((m) => (
                      <li key={m.modelId} {...stylex.props(sync.row)}>
                        <label {...stylex.props(sync.rowMain)}>
                          <input
                            type="checkbox"
                            checked={selected.has(m.modelId)}
                            onChange={() => toggle(m.modelId)}
                          />
                          <span {...stylex.props(models.id)}>{m.modelId}</span>
                          <span {...stylex.props(sync.rowLabel)}>{m.label}</span>
                        </label>
                        <span {...stylex.props(sync.rowMeta)}>
                          {!m.likelyChat && (
                            <span
                              {...stylex.props(badge.base)}
                              title="Name/modality suggests it is not a text model"
                            >
                              non-chat
                            </span>
                          )}
                          {m.contextWindow ? (
                            <span {...stylex.props(badge.base)}>{fmt(m.contextWindow)} ctx</span>
                          ) : null}
                        </span>
                      </li>
                    ))}
                  </ul>
                </section>
              )}
            </>
          )}
        </div>

        <footer {...stylex.props(formModal.footer)}>
          <div>{selected.size > 0 && `${selected.size} selected`}</div>
          <div {...stylex.props(formModal.footerActions)}>
            <button {...stylex.props(btn.base)} onClick={onClose} disabled={addAct.pending}>
              Close
            </button>
            <button
              {...stylex.props(btn.base, btn.primary)}
              disabled={selectedDrafts.length === 0 || busy}
              onClick={() => void addAct.run(selectedDrafts)}
            >
              <PlusIcon size={14} />
              {addAct.pending ? 'Adding…' : `Add ${selectedDrafts.length || ''} to catalog`}
            </button>
          </div>
        </footer>
      </div>
    </div>,
    document.body,
  );
}

function ReportBlock({report: r}: {report: SyncReport}) {
  const toast = useToast();

  // A context_window the provider states can be written straight onto a custom
  // model (add_custom_model upserts by id). A built-in's window is compiled in,
  // so it is shown as drift and nothing more.
  const applyAct = useAsyncAction(
    async (draft: DiscoveredModelDraft) => {
      const {commitAddDiscoveredModels} = await import('../../relay/AddDiscoveredModelsMutation');
      await commitAddDiscoveredModels([draft]);
      toast.push(`Updated context window for ${draft.id}`, 'success');
    },
    {onError: (e) => toast.push(e.message, 'error')},
  );

  const newCount = r.newModels.length;

  return (
    <section {...stylex.props(sync.section)}>
      <h3 {...stylex.props(sync.heading)}>
        {r.provider}
        <span {...stylex.props(page.count)}>{r.offered} offered</span>
        {r.skipped ? (
          <span {...stylex.props(badge.base, settingsBadge.warn)}>skipped</span>
        ) : r.clean ? (
          <span {...stylex.props(badge.base, badge.live)}>
            <CheckIcon size={11} /> in sync
          </span>
        ) : null}
      </h3>

      {r.skipped && (
        <p {...stylex.props(sync.note, sync.noteWarn)}>
          <AlertIcon size={13} /> {r.skipped}
        </p>
      )}

      {r.missing.length > 0 && (
        <div {...stylex.props(sync.finding)}>
          <span {...stylex.props(sync.findingTitle)}>Gone — in the catalog, no longer offered</span>
          <ul {...stylex.props(sync.plain)}>
            {r.missing.map((id) => (
              <li key={id} {...stylex.props(sync.plainItem, models.id)}>
                {id}
              </li>
            ))}
          </ul>
        </div>
      )}

      {r.unreachable.length > 0 && (
        <div {...stylex.props(sync.finding)}>
          <span {...stylex.props(sync.findingTitle)}>
            Unreachable — offered, but this credential cannot call it
          </span>
          <ul {...stylex.props(sync.plain)}>
            {r.unreachable.map((u) => (
              <li key={u.modelId} {...stylex.props(sync.plainItem)}>
                <span {...stylex.props(models.id)}>{u.modelId}</span>
                <span {...stylex.props(sync.reason)}>{u.reason}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {r.windows.length > 0 && (
        <div {...stylex.props(sync.finding)}>
          <span {...stylex.props(sync.findingTitle)}>
            Context window — sizes this model’s compaction threshold
          </span>
          <ul {...stylex.props(sync.plain)}>
            {r.windows.map((w) => (
              <li key={w.modelId} {...stylex.props(sync.plainItem, models.window)}>
                <span {...stylex.props(models.id)}>{w.modelId}</span>
                <span {...stylex.props(sync.reason)}>
                  catalog {fmt(w.catalogWindow)} → provider {fmt(w.providerWindow)}
                </span>
                {w.builtin ? (
                  <span {...stylex.props(badge.base)} title="Built-in entries are compiled in">
                    built-in
                  </span>
                ) : (
                  <button
                    {...stylex.props(btn.base, btn.small, sync.rowEndBtn)}
                    disabled={applyAct.pending}
                    onClick={() =>
                      void applyAct.run({
                        id: w.modelId,
                        label: w.label,
                        provider: w.provider,
                        contextWindow: w.providerWindow,
                      })
                    }
                  >
                    Apply
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {newCount > 0 && (
        <p {...stylex.props(field.hint)}>
          {newCount} offered but not in the catalog — listed below.
        </p>
      )}
    </section>
  );
}
