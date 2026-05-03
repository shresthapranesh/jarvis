import type {
  Artifact,
  ArtifactDetail,
  Automation,
  AutomationRun,
  Conversation,
  ConversationSummary,
  CreateAutomationPayload,
  MediaAttachment,
  ModelCatalog,
  Workflow,
  WorkflowRun,
} from './types';

export async function fetchModels(): Promise<ModelCatalog> {
  const res = await fetch('/models');
  if (!res.ok) throw new Error(`Failed to fetch models: ${res.status}`);
  return res.json();
}

export async function fetchConversations(): Promise<ConversationSummary[]> {
  const res = await fetch('/conversations');
  if (!res.ok) throw new Error(`Failed to fetch conversations: ${res.status}`);
  return res.json();
}

export async function fetchConversation(id: string): Promise<Conversation> {
  const res = await fetch(`/conversations/${id}`);
  if (!res.ok) throw new Error(`Failed to fetch conversation: ${res.status}`);
  return res.json();
}

export async function startTask(
  query: string,
  model: string,
  attachments?: MediaAttachment[],
  convId?: string,
): Promise<{task_id: string; conversation_id: string}> {
  const serializedAttachments = attachments?.map((att) => ({
    type: att.type,
    name: att.name,
    mime_type: att.mimeType,
    // strip the "data:mime;base64," prefix — backend wants raw base64
    data: att.dataUrl.split(',')[1] ?? att.dataUrl,
    size: att.size,
  }));
  const res = await fetch('/run', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({
      query,
      model,
      conversation_id: convId,
      attachments: serializedAttachments?.length ? serializedAttachments : undefined,
    }),
  });
  if (!res.ok) throw new Error(`Server error ${res.status}`);
  return res.json();
}

export async function renameConversation(id: string, title: string): Promise<void> {
  const res = await fetch(`/conversations/${id}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({title}),
  });
  if (!res.ok) throw new Error(`Failed to rename conversation: ${res.status}`);
}

export async function deleteConversation(id: string): Promise<void> {
  const res = await fetch(`/conversations/${id}`, {method: 'DELETE'});
  if (!res.ok) throw new Error(`Failed to delete conversation: ${res.status}`);
}

export async function stopTask(taskId: string): Promise<void> {
  const res = await fetch(`/stop/${taskId}`, {method: 'POST'});
  if (!res.ok) throw new Error(`Failed to stop task: ${res.status}`);
}

export async function resumeTask(taskId: string, answer: string): Promise<void> {
  const res = await fetch(`/resume/${taskId}`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({answer}),
  });
  if (!res.ok) throw new Error(`Failed to resume task: ${res.status}`);
}

export async function checkHealth(): Promise<{status: string}> {
  const res = await fetch('/health');
  if (!res.ok) throw new Error('Health check failed');
  return res.json();
}

export async function listAutomations(): Promise<Automation[]> {
  const res = await fetch('/automations');
  if (!res.ok) throw new Error(`Failed to fetch automations: ${res.status}`);
  return res.json();
}

export async function createAutomation(payload: CreateAutomationPayload): Promise<Automation> {
  const res = await fetch('/automations', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to create automation: ${res.status}`);
  return res.json();
}

export async function updateAutomation(
  id: string,
  payload: CreateAutomationPayload,
): Promise<Automation> {
  const res = await fetch(`/automations/${id}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(payload),
  });
  if (!res.ok) throw new Error(`Failed to update automation: ${res.status}`);
  return res.json();
}

export async function deleteAutomation(id: string): Promise<void> {
  const res = await fetch(`/automations/${id}`, {method: 'DELETE'});
  if (!res.ok) throw new Error(`Failed to delete automation: ${res.status}`);
}

export async function triggerAutomation(id: string): Promise<{run_id: string}> {
  const res = await fetch(`/automations/${id}/trigger`, {method: 'POST'});
  if (!res.ok) throw new Error(`Failed to trigger automation: ${res.status}`);
  return res.json();
}

export async function listAutomationRuns(id: string): Promise<AutomationRun[]> {
  const res = await fetch(`/automations/${id}/runs`);
  if (!res.ok) throw new Error(`Failed to fetch runs: ${res.status}`);
  return res.json();
}

// ── Workflow API ──────────────────────────────────────────────────────────────

export async function listWorkflows(): Promise<Workflow[]> {
  const res = await fetch('/workflows');
  if (!res.ok) throw new Error(`Failed to fetch workflows: ${res.status}`);
  return res.json();
}

export async function createWorkflow(p: {
  name: string;
  description?: string | null;
  definition: string;
  notifications?: string | null;
}): Promise<Workflow> {
  const res = await fetch('/workflows', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(p),
  });
  if (!res.ok) throw new Error(`Failed to create workflow: ${res.status}`);
  return res.json();
}

export async function getWorkflow(id: string): Promise<Workflow> {
  const res = await fetch(`/workflows/${id}`);
  if (!res.ok) throw new Error(`Failed to fetch workflow: ${res.status}`);
  return res.json();
}

export async function updateWorkflow(
  id: string,
  p: {
    name?: string | null;
    description?: string | null;
    definition?: string | null;
    notifications?: string | null;
  },
): Promise<Workflow> {
  const res = await fetch(`/workflows/${id}`, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(p),
  });
  if (!res.ok) throw new Error(`Failed to update workflow: ${res.status}`);
  return res.json();
}

export async function deleteWorkflow(id: string): Promise<void> {
  const res = await fetch(`/workflows/${id}`, {method: 'DELETE'});
  if (!res.ok) throw new Error(`Failed to delete workflow: ${res.status}`);
}

export async function triggerWorkflowRun(
  workflowId: string,
  inputs: Record<string, string>,
): Promise<{run_id: string}> {
  const res = await fetch(`/workflows/${workflowId}/run`, {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({inputs}),
  });
  if (!res.ok) throw new Error(`Failed to trigger workflow: ${res.status}`);
  return res.json();
}

export async function listWorkflowRuns(workflowId: string): Promise<WorkflowRun[]> {
  const res = await fetch(`/workflows/${workflowId}/runs`);
  if (!res.ok) throw new Error(`Failed to fetch workflow runs: ${res.status}`);
  return res.json();
}

export async function getWorkflowRun(runId: string): Promise<WorkflowRun> {
  const res = await fetch(`/workflow-runs/${runId}`);
  if (!res.ok) throw new Error(`Failed to fetch workflow run: ${res.status}`);
  return res.json();
}

// ── Artifacts ────────────────────────────────────────────────────────────────

export async function listArtifacts(conversationId?: string | null): Promise<Artifact[]> {
  const url = conversationId
    ? `/artifacts?conversation_id=${encodeURIComponent(conversationId)}`
    : '/artifacts';
  const res = await fetch(url);
  if (!res.ok) throw new Error(`Failed to fetch artifacts: ${res.status}`);
  return res.json();
}

export async function fetchArtifact(id: string): Promise<ArtifactDetail> {
  const res = await fetch(`/artifacts/${id}`);
  if (!res.ok) throw new Error(`Failed to fetch artifact: ${res.status}`);
  return res.json();
}

export async function updateArtifact(
  id: string,
  body: {title?: string; content?: string},
): Promise<void> {
  const res = await fetch(`/artifacts/${id}`, {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`Failed to update artifact: ${res.status}`);
}

export async function deleteArtifact(id: string): Promise<void> {
  const res = await fetch(`/artifacts/${id}`, {method: 'DELETE'});
  if (!res.ok) throw new Error(`Failed to delete artifact: ${res.status}`);
}

export function artifactDownloadUrl(id: string): string {
  return `/artifacts/${id}/raw`;
}

export function formatRelativeTime(isoString: string): string {
  const diff = Date.now() - new Date(isoString).getTime();
  const mins = Math.floor(diff / 60_000);
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.floor(hrs / 24);
  return `${days}d ago`;
}

export function formatNextRun(isoString: string): string {
  const diff = new Date(isoString).getTime() - Date.now();
  if (diff <= 0) return 'now';
  const secs = Math.floor(diff / 1000);
  if (secs < 60) return `in ${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `in ${mins}m`;
  const hrs = Math.floor(mins / 60);
  if (hrs < 24) {
    const remMins = mins % 60;
    return remMins ? `in ${hrs}h ${remMins}m` : `in ${hrs}h`;
  }
  const days = Math.floor(hrs / 24);
  if (days < 7) {
    const remHrs = hrs % 24;
    return remHrs ? `in ${days}d ${remHrs}h` : `in ${days}d`;
  }
  return `in ${days}d`;
}

export function formatDuration(startIso: string, endIso: string | null): string | null {
  if (!endIso) return null;
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (ms < 1000) return `${ms}ms`;
  const secs = ms / 1000;
  if (secs < 60) return `${secs.toFixed(secs < 10 ? 1 : 0)}s`;
  const mins = Math.floor(secs / 60);
  const remSecs = Math.floor(secs % 60);
  return `${mins}m ${remSecs}s`;
}
