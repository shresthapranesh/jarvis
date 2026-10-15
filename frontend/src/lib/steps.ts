import type {Step} from './types';

/**
 * Derive a human-readable label for the most recent step. Used by the
 * pre-text "Working…" indicator in both chat (MessageBubble) and live
 * (routes/live) so the user can see *what* the agent is currently doing
 * without opening the activity sidebar.
 *
 * Rules (first match wins):
 *   1. Step inside a named subagent          → "Running <subagent>…"
 *   2. model_request node with tool_calls    → "Calling <tool>…"
 *   3. tools node with a tool name           → "Ran <tool>…"
 *   4. model_request node with plain text    → "Thinking…"
 *   5. Fallback                              → "Working…"
 *
 * Rule ordering matters: a `tools` call *inside* a subagent resolves to
 * rule 1 ("Running researcher…") because knowing the subagent is more
 * informative than knowing the specific tool it's running.
 */
const PREVIEW_MAX = 90;

function trimText(s: string, max = PREVIEW_MAX): string {
  return s.length > max ? s.slice(0, max) + '…' : s;
}

/**
 * Return a short raw-content preview for the working widget's second row.
 * Shows the actual data being worked on — truncated and unstyled.
 * Returns null when there is nothing meaningful to show.
 */
export function getStepPreview(step: Step | null | undefined): string | null {
  if (!step || !step.data || step.subagent) return null;
  try {
    const parsed = JSON.parse(step.data);

    // model_request with tool call — show all input values joined
    if (step.node === 'model_request' && parsed?.tool_calls?.[0]) {
      const vals = Object.values(toolArgs(parsed.tool_calls[0]))
        .filter((v) => typeof v === 'string' || typeof v === 'number')
        .map(String)
        .join(', ');
      return vals ? trimText(vals) : null;
    }

    // tools node — show the output
    if (step.node === 'tools') {
      const first = Array.isArray(parsed) ? parsed[0] : parsed;
      const out = first?.output;
      if (typeof out === 'string' && out) return trimText(out.trim());
    }

    // plain text model_request (thinking stage)
    if (step.node === 'model_request' && typeof parsed?.text === 'string' && parsed.text) {
      return trimText(parsed.text.trim());
    }
  } catch {}
  return null;
}

/** A tool call's arguments. The Rust loop records them as `args`; rows
 *  written by the old Python runtime used `input`. */
function toolArgs(call: {args?: unknown; input?: unknown} | null | undefined) {
  return (call?.args ?? call?.input ?? {}) as Record<string, unknown>;
}

function trimInput(val: unknown, max = 40): string | null {
  if (typeof val !== 'string' || !val) return null;
  return val.length > max ? val.slice(0, max) + '…' : val;
}

// "researcher:1" → "researcher #1" for display.
function prettySubagent(key: string | null | undefined): string {
  if (!key) return 'worker';
  return key.replace(/:(\d+)$/, ' #$1');
}

export function describeStep(step: Step | null | undefined): string {
  if (!step) return 'Working…';

  if (step.node === 'worker_start') {
    return `Spawning ${prettySubagent(step.subagent)}…`;
  }
  if (step.node === 'worker_done') {
    return `${prettySubagent(step.subagent)} finished`;
  }
  if (step.subagent) {
    return `Running ${prettySubagent(step.subagent)}…`;
  }

  if (step.data) {
    try {
      const parsed = JSON.parse(step.data);

      // model_request with a pending tool call — show tool name + trimmed first input arg.
      if (step.node === 'model_request' && parsed?.tool_calls?.[0]?.name) {
        const name: string = parsed.tool_calls[0].name;
        const firstVal = trimInput(Object.values(toolArgs(parsed.tool_calls[0]))[0]);
        if (firstVal) return `Calling ${name}("${firstVal}")`;
        return `Calling ${name}…`;
      }

      // tools node — either {tool, output} (single) or [{tool, ...}, …] (batch).
      if (step.node === 'tools') {
        const first = Array.isArray(parsed) ? parsed[0] : parsed;
        if (first?.tool) {
          const firstVal = trimInput(Object.values(toolArgs(first))[0]);
          if (firstVal) return `Ran ${first.tool}("${firstVal}")`;
          return `Ran ${first.tool}…`;
        }
      }

      // model_request with plain text content (no tool call) — the model
      // is composing a response but hasn't started streaming to us yet.
      if (step.node === 'model_request' && typeof parsed?.text === 'string') {
        return 'Thinking…';
      }
    } catch {
      // step.data wasn't JSON — fall through defensively.
    }
  }

  return 'Working…';
}

/**
 * One row of the activity panel: what kind of step it was, and the line of it
 * worth reading. The node name alone ("model_request", "tools") says nothing a
 * reader can use, so a tool call shows its tool and what it set out to do, an
 * output its first line, and a reply its opening.
 */
export interface StepSummary {
  kind: 'call' | 'output' | 'reply' | 'other';
  label: string;
  detail: string | null;
}

function firstLine(s: string): string | null {
  return (
    s
      .split('\n')
      .map((l) => l.trim())
      .find(Boolean) ?? null
  );
}

/** A `run_cell` body's leading comment if it has one, else its first statement. */
function codeHeadline(code: string): string | null {
  const line = firstLine(code);
  return line && line.startsWith('#') ? line.replace(/^#+\s*/, '') || null : line;
}

export function summarizeStep(step: Step): StepSummary {
  let parsed: any = null;
  try {
    parsed = step.data ? JSON.parse(step.data) : null;
  } catch {}

  if (step.node === 'model_request') {
    const calls = Array.isArray(parsed?.tool_calls) ? parsed.tool_calls : [];
    if (calls[0]?.name) {
      const args = toolArgs(calls[0]);
      const detail =
        typeof args.code === 'string'
          ? codeHeadline(args.code)
          : (Object.values(args).find((v): v is string => typeof v === 'string') ?? null);
      const more = calls.length > 1 ? ` +${calls.length - 1}` : '';
      return {kind: 'call', label: `${calls[0].name}${more}`, detail: detail && firstLine(detail)};
    }
    if (typeof parsed?.text === 'string') {
      return {
        kind: 'reply',
        label: 'reply',
        detail: firstLine(parsed.text.replace(/[#*_`>]/g, '')),
      };
    }
  }
  if (step.node === 'tools') {
    const first = Array.isArray(parsed) ? parsed[0] : parsed;
    const out = first?.output;
    return {
      kind: 'output',
      label: 'output',
      detail: typeof out === 'string' ? firstLine(out) : null,
    };
  }
  if (step.node === 'worker_done') {
    return {kind: 'other', label: 'finished', detail: null};
  }
  return {kind: 'other', label: step.node, detail: null};
}
