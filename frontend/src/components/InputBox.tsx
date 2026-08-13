import {useEffect, useRef, useState} from 'react';

import {useIsMobile} from '../hooks/useIsMobile';
import {useModels} from '../hooks/useModels';
import {useWhisperSTT} from '../hooks/useWhisperSTT';
import type {MediaAttachment, PersistedDocument} from '../lib/types';
import {refreshConversationList} from '../relay/ConversationListQuery';
import {commitUpdateConversation} from '../relay/UpdateConversationMutation';

interface Props {
  onSubmit: (query: string, model: string, attachments: MediaAttachment[]) => void;
  disabled?: boolean;
  onStop?: () => void;
  artifactCount?: number;
  artifactPanelOpen?: boolean;
  onToggleArtifacts?: () => void;
  conversationId?: string;
  initialModel?: string;
  persistedDocuments?: PersistedDocument[];
  onDeletePersistedDocument?: (docId: string) => void;
  // Incognito toggle — only wired on the new-chat surface (index page). When
  // provided, an eye-off button lets the user start the conversation ephemeral.
  incognito?: boolean;
  onToggleIncognito?: () => void;
}

function fileTypeCategory(mimeType: string): 'image' | 'audio' | 'video' | 'document' {
  if (mimeType.startsWith('image/')) return 'image';
  if (mimeType.startsWith('audio/')) return 'audio';
  if (mimeType.startsWith('video/')) return 'video';
  return 'document';
}

export function InputBox({
  onSubmit,
  disabled = false,
  onStop,
  artifactCount = 0,
  artifactPanelOpen = false,
  onToggleArtifacts,
  conversationId,
  initialModel,
  persistedDocuments,
  onDeletePersistedDocument,
  incognito = false,
  onToggleIncognito,
}: Props) {
  const {data: catalog} = useModels();
  const isMobile = useIsMobile();
  const [model, setModel] = useState('');
  const [attachments, setAttachments] = useState<MediaAttachment[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const {listening, interimText, startListening, stopListening} = useWhisperSTT(
    (text) => {
      const el = textareaRef.current;
      if (!el) return;
      el.value = el.value ? el.value + ' ' + text : text;
      handleInput();
      el.focus();
    },
  );

  // Seed the model: prefer the per-conversation `initialModel`, then fall back
  // to the catalog default. Re-runs when the user navigates between
  // conversations so the dropdown reflects the conversation we're in.
  useEffect(() => {
    if (initialModel) {
      setModel(initialModel);
    } else if (catalog && !model) {
      setModel(catalog.default);
    }
  }, [catalog, initialModel]); // eslint-disable-line react-hooks/exhaustive-deps

  async function handleModelChange(newModel: string) {
    setModel(newModel);
    if (conversationId && newModel) {
      try {
        await commitUpdateConversation(conversationId, {model: newModel});
        await refreshConversationList();
      } catch (err) {
        console.error('Failed to persist model change:', err);
      }
    }
  }

  function handleInput() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function send() {
    const query = textareaRef.current?.value.trim();
    if ((!query && attachments.length === 0) || disabled || !model) return;
    onSubmit(query ?? '', model, attachments);
    const el = textareaRef.current!;
    el.value = '';
    el.style.height = 'auto';
    setAttachments([]);
  }

  function handleFiles(files: FileList | null) {
    if (!files) return;
    Array.from(files).forEach((file) => {
      const reader = new FileReader();
      reader.onload = (e) => {
        const dataUrl = e.target?.result as string;
        const attachment: MediaAttachment = {
          id: crypto.randomUUID(),
          type: fileTypeCategory(file.type),
          name: file.name,
          mimeType: file.type,
          dataUrl,
          size: file.size,
        };
        setAttachments((prev) => [...prev, attachment]);
      };
      reader.readAsDataURL(file);
    });
    // reset so the same file can be re-attached after removal
    if (fileInputRef.current) fileInputRef.current.value = '';
  }

  function removeAttachment(id: string) {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }

  return (
    <div className="input-wrap">
      <div className={`input-card${disabled ? ' input-card--disabled' : ''}${incognito ? ' input-card--incognito' : ''}`}>
        {(attachments.length > 0 || (persistedDocuments && persistedDocuments.length > 0)) && (
          <div className="attachment-strip">
            {persistedDocuments?.map((doc) => (
              <div key={`saved-${doc.id}`} className="attachment-thumb attachment-thumb--saved" title={`${doc.filename} (saved to conversation)`}>
                <div className="attachment-thumb-icon">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                    <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                    <polyline points="14 2 14 8 20 8" />
                    <line x1="16" y1="13" x2="8" y2="13" />
                    <line x1="16" y1="17" x2="8" y2="17" />
                    <polyline points="10 9 9 9 8 9" />
                  </svg>
                  <span className="attachment-thumb-name">{doc.filename}</span>
                </div>
                {onDeletePersistedDocument && (
                  <button
                    className="attachment-remove"
                    onClick={() => onDeletePersistedDocument(doc.id)}
                    title="Remove from conversation"
                    type="button"
                  >
                    ✕
                  </button>
                )}
              </div>
            ))}
            {attachments.map((att) => (
              <div key={att.id} className="attachment-thumb">
                {att.type === 'image' ? (
                  <img src={att.dataUrl} alt={att.name} />
                ) : (
                  <div className="attachment-thumb-icon">
                    {att.type === 'audio' ? (
                      <svg
                        width="18"
                        height="18"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >
                        <path d="M9 18V5l12-2v13" />
                        <circle cx="6" cy="18" r="3" />
                        <circle cx="18" cy="16" r="3" />
                      </svg>
                    ) : att.type === 'document' ? (
                      <svg
                        width="18"
                        height="18"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                        <polyline points="14 2 14 8 20 8" />
                        <line x1="16" y1="13" x2="8" y2="13" />
                        <line x1="16" y1="17" x2="8" y2="17" />
                        <polyline points="10 9 9 9 8 9" />
                      </svg>
                    ) : (
                      <svg
                        width="18"
                        height="18"
                        viewBox="0 0 24 24"
                        fill="none"
                        stroke="currentColor"
                        strokeWidth="2"
                        strokeLinecap="round"
                        strokeLinejoin="round"
                      >
                        <rect x="2" y="2" width="20" height="20" rx="2.18" ry="2.18" />
                        <line x1="7" y1="2" x2="7" y2="22" />
                        <line x1="17" y1="2" x2="17" y2="22" />
                        <line x1="2" y1="12" x2="22" y2="12" />
                        <line x1="2" y1="7" x2="7" y2="7" />
                        <line x1="2" y1="17" x2="7" y2="17" />
                        <line x1="17" y1="17" x2="22" y2="17" />
                        <line x1="17" y1="7" x2="22" y2="7" />
                      </svg>
                    )}
                    <span className="attachment-thumb-name">{att.name}</span>
                  </div>
                )}
                <button
                  className="attachment-remove"
                  onClick={() => removeAttachment(att.id)}
                  title="Remove"
                  type="button"
                >
                  ✕
                </button>
              </div>
            ))}
          </div>
        )}

        <textarea
          ref={(el) => {
            textareaRef.current = el;
            el?.focus();
          }}
          className="input-textarea"
          rows={1}
          placeholder={incognito ? 'Ask anything… (incognito — not saved)' : 'Ask anything…'}
          disabled={disabled}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
        />

        <div className="input-footer">
          <button
            type="button"
            className="attach-btn"
            title="Attach file"
            disabled={disabled}
            onClick={() => fileInputRef.current?.click()}
          >
            <svg
              width="13"
              height="13"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              strokeLinecap="round"
              strokeLinejoin="round"
            >
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>

          {onToggleIncognito && (
            <button
              type="button"
              className={`attach-btn${incognito ? ' attach-btn--active' : ''}`}
              title={incognito ? 'Incognito on — this chat won’t be saved' : 'Start an incognito chat (not saved)'}
              aria-pressed={incognito}
              disabled={disabled}
              onClick={onToggleIncognito}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
            </button>
          )}

          <button
            type="button"
            className={`attach-btn${listening ? ' mic-btn--active' : ''}`}
            title={listening ? 'Stop recording' : 'Voice input'}
            disabled={disabled || interimText === 'Transcribing…'}
            onClick={() => (listening ? stopListening() : void startListening())}
          >
            {interimText === 'Transcribing…' ? (
              <svg
                className="mic-spinner"
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M21 12a9 9 0 1 1-6.219-8.56" />
              </svg>
            ) : (
              <svg
                width="13"
                height="13"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <rect x="9" y="2" width="6" height="12" rx="3" />
                <path d="M5 10a7 7 0 0 0 14 0" />
                <line x1="12" y1="19" x2="12" y2="22" />
                <line x1="8" y1="22" x2="16" y2="22" />
              </svg>
            )}
          </button>

          {onToggleArtifacts && (
            <button
              type="button"
              className={`attach-btn attach-btn--artifact${artifactPanelOpen ? ' attach-btn--active' : ''}${artifactCount > 0 ? ' attach-btn--has-badge' : ''}`}
              title={artifactPanelOpen ? 'Close artifacts' : `Artifacts${artifactCount > 0 ? ` (${artifactCount})` : ''}`}
              onClick={onToggleArtifacts}
            >
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
              </svg>
              {artifactCount > 0 && <span className="artifact-btn-count">{artifactCount}</span>}
            </button>
          )}

          <input
            ref={fileInputRef}
            type="file"
            accept="image/*,audio/*,video/*,application/pdf,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/plain,text/csv,text/markdown,.pdf,.doc,.docx,.xls,.xlsx,.txt,.csv,.md,.rtf"
            multiple
            style={{display: 'none'}}
            onChange={(e) => handleFiles(e.target.files)}
          />

          <select
            className="model-input"
            value={model}
            onChange={(e) => void handleModelChange(e.target.value)}
            disabled={!catalog}
            title={catalog ? undefined : 'Loading models…'}
          >
            {!catalog && <option value="">Loading…</option>}
            {catalog?.available.map((m) => (
              <option key={m.id} value={m.id}>
                {m.label}
              </option>
            ))}
          </select>

          {/* On touch there is no Enter/Shift+Enter to describe, and at the 16px
              control size the hint pushes the send button off screen. Live
              speech interim text still shows — that one is not keyboard advice. */}
          <span className="input-hint">
            {interimText || (isMobile ? '' : 'Enter · Shift+Enter for newline')}
          </span>

          {onStop && disabled ? (
            <button className="send-btn stop-btn" onClick={onStop} title="Stop">
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="currentColor"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <rect x="6" y="6" width="12" height="12" rx="2" ry="2" />
              </svg>
            </button>
          ) : (
            <button className="send-btn" onClick={send} disabled={disabled} title="Send">
              <svg
                width="14"
                height="14"
                viewBox="0 0 24 24"
                fill="none"
                stroke="currentColor"
                strokeWidth="2.5"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <line x1="22" y1="2" x2="11" y2="13" />
                <polygon points="22 2 15 22 11 13 2 9 22 2" />
              </svg>
            </button>
          )}
        </div>
      </div>
    </div>
  );
}
