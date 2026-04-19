import {useQuery, useMutation, useQueryClient} from '@tanstack/react-query';
import {createFileRoute} from '@tanstack/react-router';
import {marked} from 'marked';
import {useState} from 'react';

import {AutomationForm} from '../components/AutomationForm';
import {useAutomationStream} from '../hooks/useAutomationStream';
import {
  listAutomations,
  createAutomation,
  updateAutomation,
  deleteAutomation,
  triggerAutomation,
  listAutomationRuns,
  formatRelativeTime,
} from '../lib/api';
import type {Automation, AutomationRun, CreateAutomationPayload} from '../lib/types';

export const Route = createFileRoute('/automation')({component: AutomationPage});

// ── Individual run row ────────────────────────────────────────────────────────

function RunRow({run}: {run: AutomationRun}) {
  const [open, setOpen] = useState(false);
  const text = run.output ?? run.error ?? '';

  return (
    <div className="run-row">
      <div className="run-row-header" onClick={() => text && setOpen((o) => !o)}>
        <span className={`run-status run-status--${run.status}`}>{run.status}</span>
        <span className="run-meta">
          {run.triggered_by} · {formatRelativeTime(run.started_at)}
          {run.finished_at &&
            ` · ${Math.round((new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000)}s`}
        </span>
        {text && <span className="run-toggle-hint">{open ? '▲' : '▼'}</span>}
      </div>
      {open && text && (
        <div
          className="run-output agent-bubble"
          dangerouslySetInnerHTML={{__html: marked.parse(text) as string}}
        />
      )}
    </div>
  );
}

// ── Run history panel ─────────────────────────────────────────────────────────

function RunHistory({automationId}: {automationId: string}) {
  const {data: runs = [], isLoading} = useQuery({
    queryKey: ['automation-runs', automationId],
    queryFn: () => listAutomationRuns(automationId),
    staleTime: 10_000,
  });

  if (isLoading) return <div className="run-empty">Loading runs…</div>;
  if (runs.length === 0) return <div className="run-empty">No runs yet.</div>;

  return (
    <div className="run-list">
      {runs.map((r) => (
        <RunRow key={r.id} run={r} />
      ))}
    </div>
  );
}

// ── Live output after manual trigger ─────────────────────────────────────────

function LiveOutput({runId, automationId}: {runId: string; automationId: string}) {
  const {streaming, text, error} = useAutomationStream(runId, automationId);

  if (error) return <div className="error-bubble run-live-error">⚠ {error}</div>;

  return (
    <div className={`agent-bubble run-live-output${streaming ? ' streaming' : ''}`}>
      {text ? (
        <div dangerouslySetInnerHTML={{__html: marked.parse(text) as string}} />
      ) : (
        <div className="thinking">
          <div className="thinking-dots">
            <span />
            <span />
            <span />
          </div>
        </div>
      )}
      {streaming && <span className="cursor" />}
    </div>
  );
}

// ── Automation row ────────────────────────────────────────────────────────────

function AutomationRow({
  auto,
  onEdit,
  onDeleted,
}: {
  auto: Automation;
  onEdit: (a: Automation) => void;
  onDeleted: () => void;
}) {
  const queryClient = useQueryClient();
  const [showHistory, setShowHistory] = useState(false);
  const [activeRunId, setActiveRunId] = useState<string | null>(null);
  const [triggerError, setTriggerError] = useState<string | null>(null);

  const toggleMutation = useMutation({
    mutationFn: () =>
      updateAutomation(auto.id, {
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
      }),
    onSuccess: () => queryClient.invalidateQueries({queryKey: ['automations']}),
  });

  const deleteMutation = useMutation({
    mutationFn: () => deleteAutomation(auto.id),
    onSuccess: () => {
      queryClient.invalidateQueries({queryKey: ['automations']});
      onDeleted();
    },
  });

  async function handleTrigger() {
    setTriggerError(null);
    setActiveRunId(null);
    try {
      const {run_id} = await triggerAutomation(auto.id);
      setActiveRunId(run_id);
      setShowHistory(true);
    } catch (err) {
      setTriggerError((err as Error).message);
    }
  }

  return (
    <div className="automation-row">
      <div className="automation-row-main">
        <div className="automation-info">
          <span className="automation-name">{auto.name}</span>
          {auto.description && <span className="automation-desc">{auto.description}</span>}
          <div className="automation-badges">
            <span className={`automation-badge automation-badge--${auto.input_type}`}>
              {auto.input_type}
            </span>
            {auto.schedule ? (
              <span className="automation-badge automation-badge--schedule">{auto.schedule}</span>
            ) : (
              <span className="automation-badge automation-badge--adhoc">ad-hoc</span>
            )}
          </div>
        </div>

        <div className="automation-actions">
          <label className="auto-toggle" title={auto.enabled ? 'Disable' : 'Enable'}>
            <input
              type="checkbox"
              checked={auto.enabled}
              onChange={() => toggleMutation.mutate()}
              disabled={toggleMutation.isPending}
            />
            <span className="auto-toggle-track" />
          </label>

          <button
            className="automation-btn"
            title="Show run history"
            onClick={() => setShowHistory((h) => !h)}
          >
            {showHistory ? '▲ Runs' : '▼ Runs'}
          </button>

          <button
            className="automation-btn automation-btn--primary"
            title="Trigger now"
            onClick={handleTrigger}
          >
            ▶ Run
          </button>

          <button className="automation-btn" title="Edit" onClick={() => onEdit(auto)}>
            ✎
          </button>

          <button
            className="automation-btn automation-btn--danger"
            title="Delete"
            onClick={() => deleteMutation.mutate()}
            disabled={deleteMutation.isPending}
          >
            ✕
          </button>
        </div>
      </div>

      {triggerError && (
        <div className="error-bubble" style={{margin: '8px 14px'}}>
          ⚠ {triggerError}
        </div>
      )}

      {activeRunId && (
        <div style={{padding: '0 14px 10px'}}>
          <LiveOutput runId={activeRunId} automationId={auto.id} />
        </div>
      )}

      {showHistory && (
        <div className="run-history">
          <RunHistory automationId={auto.id} />
        </div>
      )}
    </div>
  );
}

// ── Automation form panel (slide-in) ──────────────────────────────────────────

function AutomationFormPanel({
  editing,
  onClose,
}: {
  editing: Automation | null;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();

  async function handleSave(payload: CreateAutomationPayload) {
    if (editing) {
      await updateAutomation(editing.id, payload);
    } else {
      await createAutomation(payload);
    }
    await queryClient.invalidateQueries({queryKey: ['automations']});
    onClose();
  }

  return (
    <>
      <div className="auto-panel-backdrop" onClick={onClose} />
      <div className="auto-panel">
        <div className="auto-panel-header">
          <span>{editing ? 'Edit Automation' : 'New Automation'}</span>
          <button className="sidebar-close" onClick={onClose}>
            ✕
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
  const {
    data: automations = [],
    isLoading,
    error,
  } = useQuery({
    queryKey: ['automations'],
    queryFn: listAutomations,
    staleTime: 15_000,
  });

  const [showForm, setShowForm] = useState(false);
  const [editingAuto, setEditingAuto] = useState<Automation | null>(null);

  function openCreate() {
    setEditingAuto(null);
    setShowForm(true);
  }

  function openEdit(auto: Automation) {
    setEditingAuto(auto);
    setShowForm(true);
  }

  function closeForm() {
    setShowForm(false);
    setEditingAuto(null);
  }

  return (
    <div className="automation-page">
      <div className="automation-header">
        <span className="automation-page-title">Automations</span>
        <button className="auto-new-btn" onClick={openCreate}>
          + New
        </button>
      </div>

      <div className="automation-list-container">
        {isLoading && <div className="auto-empty">Loading…</div>}
        {error && <div className="error-bubble auto-empty">⚠ {(error as Error).message}</div>}
        {!isLoading && automations.length === 0 && (
          <div className="auto-empty">
            No automations yet.
            <button className="auto-new-btn" style={{marginLeft: 12}} onClick={openCreate}>
              Create one
            </button>
          </div>
        )}
        <div className="automation-list">
          {automations.map((auto) => (
            <AutomationRow key={auto.id} auto={auto} onEdit={openEdit} onDeleted={() => {}} />
          ))}
        </div>
      </div>

      {showForm && <AutomationFormPanel editing={editingAuto} onClose={closeForm} />}
    </div>
  );
}
