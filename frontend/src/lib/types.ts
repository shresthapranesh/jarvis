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

export type AutomationInputType = 'prompt' | 'code' | 'webhook';

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
  created_at: string;
  updated_at: string;
}

export type AutomationRunStatus = 'running' | 'done' | 'error';

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
