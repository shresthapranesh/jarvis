import * as stylex from '@stylexjs/stylex';
import {createFileRoute} from '@tanstack/react-router';
import {useEffect, useMemo, useState} from 'react';
import {useLazyLoadQuery} from 'react-relay';

import type {AutomationListQuery as TAutomationListQuery} from '../__generated__/AutomationListQuery.graphql';
import {AutomationForm} from '../components/AutomationForm';
import {AutomationRunsPanel} from '../components/AutomationRunsPanel';
import {ConfirmDialog} from '../components/ConfirmDialog';
import {
  BoltIcon,
  CalendarIcon,
  ChevronDownIcon,
  ClockIcon,
  CodeIcon,
  EditIcon,
  EyeIcon,
  PlayIcon,
  PlusIcon,
  SearchIcon,
  TrashIcon,
  WebhookIcon,
  XIcon,
} from '../components/icons';
import {QueryBoundary, useQueryRetry} from '../components/QueryBoundary';
import {closeBtn, field, page} from '../components/ui';
import {useAsyncAction} from '../hooks/useAsyncAction';
import {usePollingRefresh} from '../hooks/usePollingRefresh';
import {formatNextRun, formatRelativeTime} from '../lib/api';
import {useToast} from '../lib/toast';
import type {Automation, AutomationInputType, CreateAutomationPayload} from '../lib/types';
import {
  automationListQuery,
  mapAutomation,
  refreshAutomationList,
} from '../relay/AutomationListQuery';
import {refreshAutomationRuns} from '../relay/AutomationRunsQuery';
import {commitCreateAutomation} from '../relay/CreateAutomationMutation';
import {commitDeleteAutomation} from '../relay/DeleteAutomationMutation';
import {commitTriggerAutomation} from '../relay/TriggerAutomationMutation';
import {commitUpdateAutomation} from '../relay/UpdateAutomationMutation';
import {
  card,
  chip,
  confirmWarn,
  empty,
  filters,
  header,
  kpi,
  list,
  newBtn,
  panel,
  rail as railStyles,
  statusDot,
  typeIcon,
} from './automation.styles';

export const Route = createFileRoute('/automation')({component: AutomationRoute});

function AutomationRoute() {
  return (
    <QueryBoundary
      label="Failed to load automations"
      fallback={<div {...stylex.props(list.emptyMsg)}>Loading…</div>}
    >
      <AutomationPage />
    </QueryBoundary>
  );
}

type TypeFilter = 'all' | AutomationInputType;
type GroupBy = 'none' | 'type' | 'schedule';

function TypeIcon({type, size = 14}: {type: AutomationInputType; size?: number}) {
  if (type === 'prompt') return <BoltIcon size={size} />;
  if (type === 'code') return <CodeIcon size={size} />;
  if (type === 'monitor') return <EyeIcon size={size} />;
  return <WebhookIcon size={size} />;
}

type RailVariant = 'off' | 'run' | 'err' | 'ok' | 'idle';

function railVariant(auto: Automation): RailVariant {
  if (!auto.enabled) return 'off';
  if (auto.last_run_status === 'running') return 'run';
  if (auto.last_run_status === 'error') return 'err';
  if (auto.last_run_status === 'done' || auto.last_run_status === 'no_change') return 'ok';
  return 'idle';
}

type RunStatus = keyof typeof statusDot;

// ── Card ──────────────────────────────────────────────────────────────────────

interface CardProps {
  auto: Automation;
  onOpen: (auto: Automation) => void;
  onEdit: (auto: Automation) => void;
  onDelete: (auto: Automation) => void;
  onTrigger: (auto: Automation) => void;
  onToggle: (auto: Automation) => void;
}

function AutomationCard({auto, onOpen, onEdit, onDelete, onTrigger, onToggle}: CardProps) {
  const rail = railVariant(auto);
  const [now, setNow] = useState(Date.now());

  // Refresh "next run" countdown every 30s
  useEffect(() => {
    if (!auto.next_run_at) return;
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, [auto.next_run_at]);

  // touch `now` so it doesn't get tree-shaken
  void now;

  return (
    <div
      {...stylex.props(card.root, !auto.enabled && card.off)}
      onClick={() => onOpen(auto)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onOpen(auto);
      }}
    >
      <div {...stylex.props(railStyles.base, railStyles[rail])} aria-hidden="true" />

      <div {...stylex.props(typeIcon.base, typeIcon[auto.input_type])}>
        <TypeIcon type={auto.input_type} size={16} />
      </div>

      <div {...stylex.props(card.main)}>
        <div {...stylex.props(card.titlebar)}>
          <span {...stylex.props(card.name)}>{auto.name}</span>
          {!auto.enabled && <span {...stylex.props(card.pausedPill)}>Paused</span>}
        </div>
        {auto.description && <div {...stylex.props(card.desc)}>{auto.description}</div>}
        <div {...stylex.props(card.badges)}>
          <span {...stylex.props(chip.base, chip[auto.input_type])}>{auto.input_type}</span>
          {auto.schedule ? (
            <span {...stylex.props(chip.base, chip.schedule)} title={`cron: ${auto.schedule}`}>
              <CalendarIcon size={11} />
              {auto.schedule}
            </span>
          ) : (
            <span {...stylex.props(chip.base, chip.adhoc)}>ad-hoc</span>
          )}
        </div>
      </div>

      <div {...stylex.props(card.metaCol)}>
        {auto.next_run_at && auto.enabled && (
          <div {...stylex.props(card.metaLine, card.metaNext)}>
            <ClockIcon size={11} />
            <span>Next {formatNextRun(auto.next_run_at)}</span>
          </div>
        )}
        {auto.last_run_at && (
          <div {...stylex.props(card.metaLine)}>
            <span
              {...stylex.props(
                statusDot.base,
                statusDot[(auto.last_run_status ?? 'done') as RunStatus],
              )}
            />
            <span>Last {formatRelativeTime(auto.last_run_at)}</span>
          </div>
        )}
        {!auto.last_run_at && <div {...stylex.props(card.metaLine, card.metaNever)}>Never run</div>}
        {(auto.total_count_7d ?? 0) > 0 && (
          <div {...stylex.props(card.metaLine, card.metaStats)}>
            {auto.success_count_7d ?? 0}/{auto.total_count_7d} ok · 7d
          </div>
        )}
      </div>

      <div {...stylex.props(card.actions)} onClick={(e) => e.stopPropagation()}>
        <button
          {...stylex.props(card.action)}
          title={auto.enabled ? 'Pause schedule' : 'Resume schedule'}
          onClick={() => onToggle(auto)}
        >
          {auto.enabled ? (
            <span {...stylex.props(card.toggle, card.toggleOn)} aria-label="Enabled">
              <span {...stylex.props(card.toggleDot)} />
            </span>
          ) : (
            <span {...stylex.props(card.toggle, card.toggleOff)} aria-label="Disabled">
              <span {...stylex.props(card.toggleDot)} />
            </span>
          )}
        </button>
        <button
          {...stylex.props(card.action, card.actionPlay)}
          title="Run now"
          onClick={() => onTrigger(auto)}
        >
          <PlayIcon size={13} />
        </button>
        <button {...stylex.props(card.action)} title="Edit" onClick={() => onEdit(auto)}>
          <EditIcon size={14} />
        </button>
        <button
          {...stylex.props(card.action, card.actionDanger)}
          title="Delete"
          onClick={() => onDelete(auto)}
        >
          <TrashIcon size={14} />
        </button>
      </div>
    </div>
  );
}

// ── Form panel (slide-in, kept structurally same) ─────────────────────────────

function AutomationFormPanel({
  editing,
  onClose,
}: {
  editing: Automation | null;
  onClose: () => void;
}) {
  const toast = useToast();

  async function handleSave(payload: CreateAutomationPayload) {
    try {
      if (editing) {
        await commitUpdateAutomation(editing.id, payload);
        toast.push('Automation updated', 'success');
      } else {
        await commitCreateAutomation(payload);
        toast.push('Automation created', 'success');
      }
      await refreshAutomationList();
      onClose();
    } catch (err) {
      toast.push((err as Error).message || 'Failed to save', 'error');
    }
  }

  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  return (
    <>
      <div {...stylex.props(panel.backdrop)} onClick={onClose} />
      <div {...stylex.props(panel.root)}>
        <div {...stylex.props(panel.header)}>
          <span>{editing ? 'Edit Automation' : 'New Automation'}</span>
          <button {...stylex.props(closeBtn.base)} onClick={onClose} aria-label="Close">
            <XIcon size={14} />
          </button>
        </div>
        <div {...stylex.props(panel.body)}>
          <AutomationForm
            initialValues={editing ?? undefined}
            onSave={handleSave}
            onCancel={onClose}
          />
        </div>
      </div>
    </>
  );
}

// ── Main page ─────────────────────────────────────────────────────────────────

function AutomationPage() {
  const toast = useToast();

  const data = useLazyLoadQuery<TAutomationListQuery>(
    automationListQuery,
    {},
    {fetchPolicy: 'store-and-network', fetchKey: useQueryRetry()},
  );
  const automations = useMemo(() => data.automations.map(mapAutomation), [data.automations]);

  // Scheduled runs change last_run_status without anyone touching the page.
  usePollingRefresh(refreshAutomationList, 30_000);

  const [showForm, setShowForm] = useState(false);
  const [editingAuto, setEditingAuto] = useState<Automation | null>(null);
  const [openedAuto, setOpenedAuto] = useState<Automation | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Automation | null>(null);

  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all');
  const [groupBy, setGroupBy] = useState<GroupBy>('none');

  const toggleAction = useAsyncAction(
    async (auto: Automation) => {
      await commitUpdateAutomation(auto.id, {
        name: auto.name,
        description: auto.description,
        input_type: auto.input_type,
        prompt_text: auto.prompt_text,
        model: auto.model,
        code_text: auto.code_text,
        webhook_url: auto.webhook_url,
        webhook_method: auto.webhook_method,
        webhook_headers: auto.webhook_headers,
        webhook_body: auto.webhook_body,
        schedule: auto.schedule,
        enabled: !auto.enabled,
        notifications: auto.notifications,
      });
      await refreshAutomationList();
      toast.push(auto.enabled ? 'Automation paused' : 'Automation enabled', 'success');
    },
    {onError: (err) => toast.push(err.message || 'Failed to update', 'error')},
  );

  const triggerAction = useAsyncAction(
    async (auto: Automation) => {
      await commitTriggerAutomation(auto.id);
      setOpenedAuto(auto);
      toast.push(`Started "${auto.name}"`, 'info');
      void refreshAutomationRuns(auto.id);
    },
    {onError: (err) => toast.push(err.message || 'Failed to trigger', 'error')},
  );

  const deleteAction = useAsyncAction(
    async (auto: Automation) => {
      await commitDeleteAutomation(auto.id);
      await refreshAutomationList();
      if (openedAuto?.id === auto.id) setOpenedAuto(null);
      setConfirmDelete(null);
      toast.push(`Deleted "${auto.name}"`, 'success');
    },
    {onError: (err) => toast.push(err.message || 'Failed to delete', 'error')},
  );

  function openCreate() {
    setEditingAuto(null);
    setOpenedAuto(null);
    setShowForm(true);
  }

  function openEdit(auto: Automation) {
    setEditingAuto(auto);
    setOpenedAuto(null);
    setShowForm(true);
  }

  function closeForm() {
    setShowForm(false);
    setEditingAuto(null);
  }

  function openRunsPanel(auto: Automation) {
    setShowForm(false);
    setOpenedAuto(auto);
  }

  // ── Derived: filtered + grouped ──────────────────────────────────────────
  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    return automations.filter((a) => {
      if (typeFilter !== 'all' && a.input_type !== typeFilter) return false;
      if (q) {
        const hay = `${a.name} ${a.description ?? ''}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
  }, [automations, search, typeFilter]);

  const grouped = useMemo(() => {
    if (groupBy === 'none') return [{label: '', items: filtered}];
    const map = new Map<string, Automation[]>();
    for (const a of filtered) {
      const key = groupBy === 'type' ? a.input_type : a.schedule ? 'Scheduled' : 'Ad-hoc';
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(a);
    }
    const order =
      groupBy === 'type' ? ['prompt', 'monitor', 'code', 'webhook'] : ['Scheduled', 'Ad-hoc'];
    return order.filter((k) => map.has(k)).map((k) => ({label: k, items: map.get(k)!}));
  }, [filtered, groupBy]);

  // ── KPIs ─────────────────────────────────────────────────────────────────
  const kpiTotal = automations.length;
  const kpiActive = automations.filter((a) => a.enabled).length;
  const kpiRuns7d = automations.reduce((s, a) => s + (a.total_count_7d ?? 0), 0);
  const kpiSuccess7d = automations.reduce((s, a) => s + (a.success_count_7d ?? 0), 0);
  const successRate = kpiRuns7d > 0 ? Math.round((kpiSuccess7d / kpiRuns7d) * 100) : null;

  return (
    <div {...stylex.props(page.root)}>
      <header {...stylex.props(header.root)}>
        <div {...stylex.props(header.titleRow)}>
          <div {...stylex.props(header.titleBlock)}>
            <h1 {...stylex.props(header.title)}>Automations</h1>
            <span {...stylex.props(header.subtitle)}>Scheduled and on-demand jobs.</span>
          </div>
          <button {...stylex.props(newBtn.base)} onClick={openCreate}>
            <PlusIcon size={13} />
            <span>New automation</span>
          </button>
        </div>

        <div {...stylex.props(kpi.grid)}>
          <div {...stylex.props(kpi.chip)}>
            <div {...stylex.props(kpi.value)}>{kpiTotal}</div>
            <div {...stylex.props(kpi.label)}>Total</div>
          </div>
          <div {...stylex.props(kpi.chip)}>
            <div {...stylex.props(kpi.value)}>
              {kpiActive}
              <span {...stylex.props(kpi.suffix)}>/ {kpiTotal}</span>
            </div>
            <div {...stylex.props(kpi.label)}>Active</div>
          </div>
          <div {...stylex.props(kpi.chip)}>
            <div {...stylex.props(kpi.value)}>{kpiRuns7d}</div>
            <div {...stylex.props(kpi.label)}>Runs · 7d</div>
          </div>
          <div {...stylex.props(kpi.chip)}>
            <div {...stylex.props(kpi.value)}>{successRate !== null ? `${successRate}%` : '—'}</div>
            <div {...stylex.props(kpi.label)}>Success · 7d</div>
          </div>
        </div>

        {automations.length > 0 && (
          <div {...stylex.props(filters.bar)}>
            <div {...stylex.props(filters.search)}>
              <SearchIcon size={13} />
              <input
                {...stylex.props(filters.searchInput)}
                type="text"
                placeholder="Search by name or description…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              {search && (
                <button
                  {...stylex.props(filters.searchClear)}
                  onClick={() => setSearch('')}
                  aria-label="Clear search"
                >
                  <XIcon size={11} />
                </button>
              )}
            </div>
            <div {...stylex.props(filters.pills)}>
              {(['all', 'prompt', 'monitor', 'code', 'webhook'] as TypeFilter[]).map((t) => (
                <button
                  key={t}
                  {...stylex.props(filters.pill, typeFilter === t && filters.pillOn)}
                  onClick={() => setTypeFilter(t)}
                >
                  {t === 'all' ? 'All' : t}
                </button>
              ))}
            </div>
            <div {...stylex.props(filters.groupBy)}>
              <label {...stylex.props(filters.groupByLabel)} htmlFor="group-by">
                Group
              </label>
              <select
                {...stylex.props(filters.groupBySelect, field.selectChrome)}
                id="group-by"
                value={groupBy}
                onChange={(e) => setGroupBy(e.target.value as GroupBy)}
              >
                <option value="none">None</option>
                <option value="type">By type</option>
                <option value="schedule">By schedule</option>
              </select>
              <ChevronDownIcon size={11} />
            </div>
          </div>
        )}
      </header>

      <div {...stylex.props(list.container)}>
        {automations.length === 0 && (
          <div {...stylex.props(empty.root)}>
            <div {...stylex.props(empty.glow)}>
              <BoltIcon size={28} />
            </div>
            <h2 {...stylex.props(empty.title)}>No automations yet</h2>
            <p {...stylex.props(empty.body)}>
              Schedule prompts, run scripts on a cron, or trigger webhooks. Anything you'd want to
              fire automatically — set it up here.
            </p>
            <button {...stylex.props(newBtn.base)} onClick={openCreate}>
              <PlusIcon size={13} />
              <span>Create your first automation</span>
            </button>
          </div>
        )}

        {automations.length > 0 && filtered.length === 0 && (
          <div {...stylex.props(list.emptyMsg)}>No automations match your filters.</div>
        )}

        {grouped.map((group) => (
          <section key={group.label || 'all'} {...stylex.props(list.group)}>
            {group.label && (
              <div {...stylex.props(list.groupLabel)}>
                <span>{group.label}</span>
                <span {...stylex.props(list.groupCount)}>{group.items.length}</span>
              </div>
            )}
            <div {...stylex.props(list.cards)}>
              {group.items.map((auto) => (
                <AutomationCard
                  key={auto.id}
                  auto={auto}
                  onOpen={openRunsPanel}
                  onEdit={openEdit}
                  onDelete={(a) => setConfirmDelete(a)}
                  onTrigger={(a) => void triggerAction.run(a)}
                  onToggle={(a) => void toggleAction.run(a)}
                />
              ))}
            </div>
          </section>
        ))}
      </div>

      {showForm && <AutomationFormPanel editing={editingAuto} onClose={closeForm} />}

      {openedAuto && !showForm && (
        <AutomationRunsPanel
          automation={openedAuto}
          onClose={() => setOpenedAuto(null)}
          onEdit={openEdit}
        />
      )}

      <ConfirmDialog
        open={confirmDelete !== null}
        title="Delete automation?"
        danger
        confirmLabel="Delete"
        requireTypedName={confirmDelete?.name ?? null}
        message={
          confirmDelete && (
            <>
              <p>
                This permanently deletes <strong>{confirmDelete.name}</strong> and all of its run
                history{' '}
                {(confirmDelete.total_count_7d ?? 0) > 0 && (
                  <>({confirmDelete.total_count_7d} runs in the last 7 days)</>
                )}
                .
              </p>
              <p {...stylex.props(confirmWarn.base)}>This cannot be undone.</p>
            </>
          )
        }
        onConfirm={() => confirmDelete && void deleteAction.run(confirmDelete)}
        onCancel={() => setConfirmDelete(null)}
      />
    </div>
  );
}
