import type {CreateAutomationPayload} from '../lib/types';

export function payloadToInput(p: CreateAutomationPayload) {
  return {
    name: p.name,
    inputType: p.input_type,
    description: p.description ?? null,
    promptText: p.prompt_text ?? null,
    model: p.model ?? null,
    codeText: p.code_text ?? null,
    webhookUrl: p.webhook_url ?? null,
    webhookMethod: p.webhook_method ?? null,
    webhookHeaders: p.webhook_headers ?? null,
    webhookBody: p.webhook_body ?? null,
    schedule: p.schedule ?? null,
    enabled: p.enabled,
    stateful: p.stateful ?? false,
    notifications: p.notifications ?? null,
  };
}
