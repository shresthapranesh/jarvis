import {useQuery, useMutation, useQueryClient} from '@tanstack/react-query';
import {createFileRoute} from '@tanstack/react-router';
import {useEffect, useMemo, useState} from 'react';

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
import {formatNextRun, formatRelativeTime} from '../lib/api';
import {useToast} from '../lib/toast';
import type {Automation, AutomationInputType, CreateAutomationPayload} from '../lib/types';
import {fetchAutomationList} from '../relay/AutomationListQuery';
import {commitCreateAutomation} from '../relay/CreateAutomationMutation';
import {commitDeleteAutomation} from '../relay/DeleteAutomationMutation';
import {commitTriggerAutomation} from '../relay/TriggerAutomationMutation';
import {commitUpdateAutomation} from '../relay/UpdateAutomationMutation';

export const Route = createFileRoute('/automation')({component: AutomationPage});

type TypeFilter = 'all' | AutomationInputType;
type GroupBy = 'none' | 'type' | 'schedule';

function TypeIcon({type, size = 14}: {type: AutomationInputType; size?: number}) {
  if (type === 'prompt') return <BoltIcon size={size} />;
  if (type === 'code') return <CodeIcon size={size} />;
  if (type === 'monitor') return <EyeIcon size={size} />;
  return <WebhookIcon size={size} />;
}

function railVariant(auto: Automation): string {
  if (!auto.enabled) return 'off';
  if (auto.last_run_status === 'running') return 'run';
  if (auto.last_run_status === 'error') return 'err';
  if (auto.last_run_status === 'done' || auto.last_run_status === 'no_change') return 'ok';
  return 'idle';
}

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
      className={`auto-card auto-card--rail-${rail}${!auto.enabled ? ' auto-card--off' : ''}`}
      onClick={() => onOpen(auto)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === 'Enter') onOpen(auto);
      }}
    >
      <div className={`auto-card-rail auto-card-rail--${rail}`} aria-hidden="true" />

      <div className={`auto-card-icon auto-card-icon--${auto.input_type}`}>
        <TypeIcon type={auto.input_type} size={16} />
      </div>

      <div className="auto-card-main">
        <div className="auto-card-titlebar">
          <span className="auto-card-name">{auto.name}</span>
          {!auto.enabled && <span className="auto-card-paused-pill">Paused</span>}
        </div>
        {auto.description && <div className="auto-card-desc">{auto.description}</div>}
        <div className="auto-card-badges">
          <span className={`auto-card-badge auto-card-badge--${auto.input_type}`}>
            {auto.input_type}
          </span>
          {auto.schedule ? (
            <span className="auto-card-badge auto-card-badge--schedule" title={`cron: ${auto.schedule}`}>
              <CalendarIcon size={11} />
              {auto.schedule}
            </span>
          ) : (
            <span className="auto-card-badge auto-card-badge--adhoc">ad-hoc</span>
          )}
        </div>
      </div>

      <div className="auto-card-meta-col">
        {auto.next_run_at && auto.enabled && (
          <div className="auto-card-meta-line auto-card-meta-line--next">
            <ClockIcon size={11} />
            <span>Next {formatNextRun(auto.next_run_at)}</span>
          </div>
        )}
        {auto.last_run_at && (
          <div className="auto-card-meta-line auto-card-meta-line--last">
            <span className={`run-status-dot run-status-dot--${auto.last_run_status ?? 'done'}`} />
            <span>Last {formatRelativeTime(auto.last_run_at)}</span>
          </div>
        )}
        {!auto.last_run_at && (
          <div className="auto-card-meta-line auto-card-meta-line--last auto-card-meta-line--never">
            Never run
          </div>
        )}
        {(auto.total_count_7d ?? 0) > 0 && (
          <div className="auto-card-meta-line auto-card-meta-line--stats">
            {auto.success_count_7d ?? 0}/{auto.total_count_7d} ok · 7d
          </div>
        )}
      </div>

      <div className="auto-card-actions" onClick={(e) => e.stopPropagation()}>
        <button
          className="auto-card-action"
          title={auto.enabled ? 'Pause schedule' : 'Resume schedule'}
          onClick={() => onToggle(auto)}
        >
          {auto.enabled ? (
            <span className="auto-card-toggle-on" aria-label="Enabled">
              <span className="auto-card-toggle-dot" />
            </span>
          ) : (
            <span className="auto-card-toggle-off" aria-label="Disabled">
              <span className="auto-card-toggle-dot" />
            </span>
          )}
        </button>
        <button
          className="auto-card-action auto-card-action--play"
          title="Run now"
          onClick={() => onTrigger(auto)}
        >
          <PlayIcon size={13} />
        </button>
        <button
          className="auto-card-action"
          title="Edit"
          onClick={() => onEdit(auto)}
        >
          <EditIcon size={14} />
        </button>
        <button
          className="auto-card-action auto-card-action--danger"
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
  const queryClient = useQueryClient();
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
      await queryClient.invalidateQueries({queryKey: ['automations']});
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
      <div className="auto-panel-backdrop" onClick={onClose} />
      <div className="auto-panel">
        <div className="auto-panel-header">
          <span>{editing ? 'Edit Automation' : 'New Automation'}</span>
          <button className="sidebar-close" onClick={onClose} aria-label="Close">
            <XIcon size={14} />
          </button>
        </div>
        <div className="auto-panel-body">
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
  const queryClient = useQueryClient();
  const toast = useToast();

  const {data: automations = [], isLoading, error} = useQuery({
    queryKey: ['automations'],
    queryFn: fetchAutomationList,
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

  const [showForm, setShowForm] = useState(false);
  const [editingAuto, setEditingAuto] = useState<Automation | null>(null);
  const [openedAuto, setOpenedAuto] = useState<Automation | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<Automation | null>(null);

  const [search, setSearch] = useState('');
  const [typeFilter, setTypeFilter] = useState<TypeFilter>('all');
  const [groupBy, setGroupBy] = useState<GroupBy>('none');

  const toggleMutation = useMutation({
    mutationFn: (auto: Automation) =>
      commitUpdateAutomation(auto.id, {
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
      }),
    onSuccess: (_d, auto) => {
      queryClient.invalidateQueries({queryKey: ['automations']});
      toast.push(auto.enabled ? 'Automation paused' : 'Automation enabled', 'success');
    },
    onError: (err: Error) => toast.push(err.message || 'Failed to update', 'error'),
  });

  const triggerMutation = useMutation({
    mutationFn: (auto: Automation) => commitTriggerAutomation(auto.id),
    onSuccess: (_d, auto) => {
      setOpenedAuto(auto);
      toast.push(`Started "${auto.name}"`, 'info');
      queryClient.invalidateQueries({queryKey: ['automation-runs', auto.id]});
    },
    onError: (err: Error) => toast.push(err.message || 'Failed to trigger', 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (auto: Automation) => commitDeleteAutomation(auto.id),
    onSuccess: (_d, auto) => {
      queryClient.invalidateQueries({queryKey: ['automations']});
      if (openedAuto?.id === auto.id) setOpenedAuto(null);
      setConfirmDelete(null);
      toast.push(`Deleted "${auto.name}"`, 'success');
    },
    onError: (err: Error) => toast.push(err.message || 'Failed to delete', 'error'),
  });

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
      const key =
        groupBy === 'type'
          ? a.input_type
          : a.schedule
          ? 'Scheduled'
          : 'Ad-hoc';
      if (!map.has(key)) map.set(key, []);
      map.get(key)!.push(a);
    }
    const order =
      groupBy === 'type' ? ['prompt', 'monitor', 'code', 'webhook'] : ['Scheduled', 'Ad-hoc'];
    return order
      .filter((k) => map.has(k))
      .map((k) => ({label: k, items: map.get(k)!}));
  }, [filtered, groupBy]);

  // ── KPIs ─────────────────────────────────────────────────────────────────
  const kpiTotal = automations.length;
  const kpiActive = automations.filter((a) => a.enabled).length;
  const kpiRuns7d = automations.reduce((s, a) => s + (a.total_count_7d ?? 0), 0);
  const kpiSuccess7d = automations.reduce((s, a) => s + (a.success_count_7d ?? 0), 0);
  const successRate = kpiRuns7d > 0 ? Math.round((kpiSuccess7d / kpiRuns7d) * 100) : null;

  return (
    <div className="automation-page">
      <header className="auto-page-header">
        <div className="auto-page-titlerow">
          <div className="auto-page-title-block">
            <h1 className="auto-page-title">Automations</h1>
            <span className="auto-page-subtitle">
              Scheduled and on-demand jobs.
            </span>
          </div>
          <button className="auto-new-btn-v2" onClick={openCreate}>
            <PlusIcon size={13} />
            <span>New automation</span>
          </button>
        </div>

        <div className="auto-kpis">
          <div className="auto-kpi">
            <div className="auto-kpi-value">{kpiTotal}</div>
            <div className="auto-kpi-label">Total</div>
          </div>
          <div className="auto-kpi">
            <div className="auto-kpi-value">
              {kpiActive}
              <span className="auto-kpi-value-suffix">/ {kpiTotal}</span>
            </div>
            <div className="auto-kpi-label">Active</div>
          </div>
          <div className="auto-kpi">
            <div className="auto-kpi-value">{kpiRuns7d}</div>
            <div className="auto-kpi-label">Runs · 7d</div>
          </div>
          <div className="auto-kpi">
            <div className="auto-kpi-value">
              {successRate !== null ? `${successRate}%` : '—'}
            </div>
            <div className="auto-kpi-label">Success · 7d</div>
          </div>
        </div>

        {automations.length > 0 && (
          <div className="auto-filters">
            <div className="auto-search">
              <SearchIcon size={13} />
              <input
                type="text"
                placeholder="Search by name or description…"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
              {search && (
                <button
                  className="auto-search-clear"
                  onClick={() => setSearch('')}
                  aria-label="Clear search"
                >
                  <XIcon size={11} />
                </button>
              )}
            </div>
            <div className="auto-filter-pills">
              {(['all', 'prompt', 'monitor', 'code', 'webhook'] as TypeFilter[]).map((t) => (
                <button
                  key={t}
                  className={`auto-filter-pill${typeFilter === t ? ' auto-filter-pill--on' : ''}`}
                  onClick={() => setTypeFilter(t)}
                >
                  {t === 'all' ? 'All' : t}
                </button>
              ))}
            </div>
            <div className="auto-group-by">
              <label htmlFor="group-by">Group</label>
              <select
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

      <div className="auto-list-container">
        {isLoading && <div className="auto-empty-msg">Loading…</div>}
        {error && (
          <div className="error-bubble auto-empty-msg">
            ⚠ {(error as Error).message}
          </div>
        )}

        {!isLoading && automations.length === 0 && (
          <div className="auto-empty-state">
            <div className="auto-empty-glow">
              <BoltIcon size={28} />
            </div>
            <h2>No automations yet</h2>
            <p>
              Schedule prompts, run scripts on a cron, or trigger webhooks. Anything
              you'd want to fire automatically — set it up here.
            </p>
            <button className="auto-new-btn-v2" onClick={openCreate}>
              <PlusIcon size={13} />
              <span>Create your first automation</span>
            </button>
          </div>
        )}

        {!isLoading && automations.length > 0 && filtered.length === 0 && (
          <div className="auto-empty-msg">
            No automations match your filters.
          </div>
        )}

        {grouped.map((group) => (
          <section key={group.label || 'all'} className="auto-group">
            {group.label && (
              <div className="auto-group-label">
                <span>{group.label}</span>
                <span className="auto-group-count">{group.items.length}</span>
              </div>
            )}
            <div className="auto-card-list">
              {group.items.map((auto) => (
                <AutomationCard
                  key={auto.id}
                  auto={auto}
                  onOpen={openRunsPanel}
                  onEdit={openEdit}
                  onDelete={(a) => setConfirmDelete(a)}
                  onTrigger={(a) => triggerMutation.mutate(a)}
                  onToggle={(a) => toggleMutation.mutate(a)}
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
                This permanently deletes <strong>{confirmDelete.name}</strong> and all
                of its run history{' '}
                {(confirmDelete.total_count_7d ?? 0) > 0 && (
                  <>({confirmDelete.total_count_7d} runs in the last 7 days)</>
                )}
                .
              </p>
              <p className="confirm-warn">This cannot be undone.</p>
            </>
          )
        }
        onConfirm={() => confirmDelete && deleteMutation.mutate(confirmDelete)}
        onCancel={() => setConfirmDelete(null)}
      />
    </div>
  );
}
