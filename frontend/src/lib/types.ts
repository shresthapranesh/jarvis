import {decodeGlobalId} from '../relay/globalId';

export interface ConversationSummary {
  id: string;
  title: string | null;
  model: string;
  created_at: string;
  message_count: number;
}

export interface Step {
  id: string;
  node: string;
  source: string;
  // Friendly subagent name ("researcher", "coder", …) when `source` is
  // "subagent"; absent or null for main-agent steps. Used by describeStep()
  // to label the live activity indicator. Not persisted to the DB — only
  // present on SSE-streamed steps, so the field is optional for historical
  // steps fetched via GET /conversations/{id}.
  subagent?: string | null;
  data: string | null;
  seq: number;
  created_at: string;
}

export interface Message {
  id: string;
  role: 'user' | 'assistant';
  content: string;
  model: string | null;
  status: string;
  created_at: string;
  steps: Step[];
}

export interface Conversation {
  id: string;
  title: string | null;
  model: string;
  created_at: string;
  messages: Message[];
}

// Shape of a Relay-fetched Message node. Used by mapMessage() below to convert
// camelCase + GlobalID to the snake_case Message interface the rest of the app
// uses. Kept structural (not bound to a generated type) so it works for both
// the pagination fragment and ad-hoc fetchQuery results.
export interface RelayMessageNode {
  id: string;
  role: string;
  content: string;
  model: string | null | undefined;
  status: string;
  createdAt: string;
  steps: ReadonlyArray<{
    id: string;
    node: string;
    source: string;
    data: string | null | undefined;
    seq: number;
    createdAt: string;
  }>;
}

export interface StreamingMessage {
  text: string;
  steps: Step[];
  done: boolean;
}

export interface MediaAttachment {
  id: string;
  type: 'image' | 'audio' | 'video' | 'document';
  name: string;
  mimeType: string;
  dataUrl: string; // full data URL (data:mime;base64,...) — for preview + sending
  size: number;
}

export interface PersistedDocument {
  id: string;
  conversation_id: string;
  message_id: string | null;
  filename: string;
  mime_type: string;
  size: number;
  created_at: string;
}

export type LogLevel = 'DEBUG' | 'INFO' | 'WARNING' | 'ERROR' | 'CRITICAL';

export interface LogRecord {
  ts: string;
  level: LogLevel;
  logger: string;
  message: string;
}

export type TodoStatus = 'pending' | 'in_progress' | 'done';

export interface TodoItem {
  text: string;
  status: TodoStatus;
}

export type AutomationInputType = 'prompt' | 'code' | 'webhook';

export type NotificationOn = 'done' | 'error' | 'both';

export interface NotificationConfig {
  id: string;
  on: NotificationOn;
}

export type NotificationChannelType = 'telegram' | 'discord';

export interface NotificationChannel {
  id: string;
  name: string;
  type: NotificationChannelType;
  target: string;
  created_at: string;
  updated_at: string;
}

export interface NotificationChannelReference {
  kind: 'automation' | 'workflow';
  id: string;
  name: string;
}

export interface Automation {
  id: string;
  name: string;
  description: string | null;
  input_type: AutomationInputType;
  prompt_text: string | null;
  model: string | null;
  code_text: string | null;
  webhook_url: string | null;
  webhook_method: string | null;
  webhook_headers: string | null;
  webhook_body: string | null;
  schedule: string | null;
  enabled: boolean;
  stateful: boolean;
  conversation_id: string | null;
  notifications: string | null;
  created_at: string;
  updated_at: string;
  // Stats fields — present on list endpoint, absent on single-record endpoints
  next_run_at?: string | null;
  last_run_status?: AutomationRunStatus | null;
  last_run_at?: string | null;
  success_count_7d?: number;
  total_count_7d?: number;
}

export type AutomationRunStatus = 'running' | 'done' | 'error' | 'stopped' | 'blocked' | 'skipped';

export interface AutomationRun {
  id: string;
  automation_id: string;
  status: AutomationRunStatus;
  triggered_by: 'schedule' | 'manual';
  output: string | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface ModelSpec {
  id: string;
  label: string;
  provider: string;
}

export interface ModelCatalog {
  default: string;
  available: ModelSpec[];
}

export interface CreateAutomationPayload {
  name: string;
  description?: string | null;
  input_type: AutomationInputType;
  prompt_text?: string | null;
  model?: string | null;
  code_text?: string | null;
  webhook_url?: string | null;
  webhook_method?: string | null;
  webhook_headers?: string | null;
  webhook_body?: string | null;
  schedule?: string | null;
  enabled: boolean;
  stateful?: boolean;
  notifications?: string | null;
}

// ── Workflow ──────────────────────────────────────────────────────────────────

export interface NodeRecord {
  node_id: string;
  node_type: string;
  label: string;
  status: 'done' | 'error' | 'running';
  inputs: Record<string, unknown>;
  outputs: Record<string, unknown> | null;
  error: string | null;
  started_at: string;
  finished_at: string | null;
  verdict?: string;
  rendered_prompt?: string;
}

export type WorkflowNodeType = 'agent' | 'conditional' | 'map' | 'start';

export interface WorkflowNodeData extends Record<string, unknown> {
  label: string;
  // agent fields
  prompt_template?: string;
  model?: string;
  output_key?: string;
  input_ports?: string[];
  // conditional fields
  condition?: string;
  input_key?: string;
  // start node fields
  initial_inputs?: Record<string, string>;
  // map fields
  items_key?: string;
  workflow_id?: string;
  sub_graph?: string; // JSON string for inline sub-graph
  result_key?: string;
  concurrency?: number;
}

export interface WorkflowRFNode {
  id: string;
  type: WorkflowNodeType;
  position: {x: number; y: number};
  data: WorkflowNodeData;
}

export interface WorkflowRFEdge {
  id: string;
  source: string;
  sourceHandle: string;
  target: string;
  targetHandle: string;
}

export interface Workflow {
  id: string;
  name: string;
  description: string | null;
  definition: string; // JSON string
  notifications: string | null;
  created_at: string;
  updated_at: string;
}

export type WorkflowRunStatus = 'running' | 'done' | 'error';

export interface WorkflowRun {
  id: string;
  workflow_id: string;
  status: WorkflowRunStatus;
  inputs: string | null;
  outputs: string | null;
  node_results: string | null; // JSON array
  error: string | null;
  started_at: string;
  finished_at: string | null;
}

// ── Artifacts ─────────────────────────────────────────────────────────────────

export interface Artifact {
  id: string;
  title: string;
  kind: string;
  conversation_id: string | null;
  message_id: string | null;
  created_at: string;
  updated_at: string;
}

export interface ArtifactDetail extends Artifact {
  content: string;
}

export interface ArtifactRef {
  id: string;
  title: string;
  action: 'created' | 'updated';
  preview?: string;
}

// ── Memory ────────────────────────────────────────────────────────────────────

export interface Memory {
  content: string;
  exists: boolean;
  modified_at: string | null;
}

export type MemoryKind = 'core' | 'fact';

export interface MemoryItem {
  id: string;
  kind: MemoryKind;
  text: string;
  updated_at: string;
}

// ── Skills ────────────────────────────────────────────────────────────────────

export interface Skill {
  id: string;
  name: string;
  description: string;
  body: string;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

// ── Running tasks (global registry) ──────────────────────────────────────────

export type TaskKind = 'chat' | 'automation' | 'workflow';

export interface RunningTask {
  id: string;
  kind: TaskKind;
  label: string;
  parent_id: string | null;
  started_at: string;
  has_interrupt: boolean;
  cancelled: boolean;
  done: boolean;
}

export type NodeExecStatus = 'pending' | 'running' | 'done' | 'error';

export interface NodeStatus {
  status: NodeExecStatus;
  label?: string;
  output?: Record<string, unknown>;
  error?: string;
  verdict?: 'true' | 'false';
  tokens?: string;
  mapProgress?: {completed: number; total: number};
}

// Translate a Relay-shaped Message node to the snake_case Message interface
// the rest of the app reads. Mirrors the per-Relay-file mapXxx() translators.
export function mapMessage(m: RelayMessageNode): Message {
  return {
    id: decodeGlobalId(m.id),
    role: m.role as 'user' | 'assistant',
    content: m.content,
    model: m.model ?? null,
    status: m.status,
    created_at: m.createdAt,
    steps: m.steps.map((s) => ({
      id: s.id,
      node: s.node,
      source: s.source,
      data: s.data ?? null,
      seq: s.seq,
      created_at: s.createdAt,
    })),
  };
}

// Convert backend definition JSON → React Flow nodes/edges
export function parseDefinition(defJson: string): {nodes: WorkflowRFNode[]; edges: WorkflowRFEdge[]} {
  try {
    const def = JSON.parse(defJson || '{"nodes":[],"edges":[]}') as {
      nodes?: Array<{id: string; type: WorkflowNodeType; label?: string; position?: {x: number; y: number}; config?: Record<string, unknown>}>;
      edges?: WorkflowRFEdge[];
    };
    const nodes: WorkflowRFNode[] = (def.nodes ?? []).map((n) => ({
      id: n.id,
      type: n.type,
      position: n.position ?? {x: 100, y: 100},
      data: {label: n.label ?? n.type, ...(n.config ?? {})} as WorkflowNodeData,
    }));
    const edges: WorkflowRFEdge[] = def.edges ?? [];
    return {nodes, edges};
  } catch {
    return {nodes: [], edges: []};
  }
}

// Convert React Flow nodes/edges → backend definition JSON string
export function serializeDefinition(nodes: WorkflowRFNode[], edges: WorkflowRFEdge[]): string {
  const backendNodes = nodes.map(({id, type, position, data}) => {
    const {label, ...config} = data;
    return {id, type, label, position, config};
  });
  return JSON.stringify({nodes: backendNodes, edges});
}
