import {useRef, useState} from 'react';

import {commitResolveApproval} from '../relay/ResolveApprovalMutation';
import {commitResumeTask} from '../relay/ResumeTaskMutation';

interface Props {
  taskId: string;
  question: string;
  /**
   * Set when the pause is a per-tool approval gate (core/tool_gate.py). The run
   * is blocked inside the tool call, not on a LangGraph interrupt, so the
   * answer goes to the durable row — `resumeTask` would find no interrupt to
   * resume and the call would stay parked until it times out.
   */
  approvalId?: string;
}

export function InterruptPrompt({taskId, question, approvalId}: Props) {
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);

  async function submit() {
    const answer = textareaRef.current?.value.trim() ?? '';
    if (!answer || submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      if (approvalId) {
        await commitResolveApproval(approvalId, answer);
      } else {
        await commitResumeTask(taskId, answer);
      }
      // The approval_resolved / interrupt_resolved event clears this prompt —
      // no local state needed.
    } catch (e) {
      setError((e as Error).message);
      setSubmitting(false);
    }
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div className="interrupt-wrap">
      <div className="interrupt-card">
        <div className="interrupt-header">
          <svg
            width="14"
            height="14"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="2"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <circle cx="12" cy="12" r="10" />
            <line x1="12" y1="8" x2="12" y2="12" />
            <line x1="12" y1="16" x2="12.01" y2="16" />
          </svg>
          <span>Agent needs your input</span>
        </div>
        <div className="interrupt-question">{question}</div>
        <textarea
          ref={(el) => {
            textareaRef.current = el;
            el?.focus();
          }}
          className="interrupt-textarea"
          rows={2}
          placeholder="Type your answer…"
          disabled={submitting}
          onKeyDown={handleKeyDown}
        />
        {error && <div className="interrupt-error">{error}</div>}
        <div className="interrupt-footer">
          <span className="interrupt-hint">Enter to submit · Shift+Enter for newline</span>
          <button type="button" className="interrupt-submit" disabled={submitting} onClick={submit}>
            {submitting ? 'Sending…' : 'Send'}
          </button>
        </div>
      </div>
    </div>
  );
}
