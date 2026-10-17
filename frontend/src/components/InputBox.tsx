import * as stylex from '@stylexjs/stylex';
import {useEffect, useRef, useState} from 'react';

import {useIsMobile} from '../hooks/useIsMobile';
import {useModels} from '../hooks/useModels';
import {effectiveEffort} from '../lib/effort';
import {useToast} from '../lib/toast';
import {formatBytes, MAX_UPLOAD_BYTES, type UploadedFile, uploadFile} from '../lib/uploads';
import {refreshConversationList} from '../relay/ConversationListQuery';
import {commitUpdateConversation} from '../relay/UpdateConversationMutation';
import {attachment, composer, control} from './InputBox.styles';
import {ModelPicker} from './ModelPicker';
import {iconBtn} from './ui';

/** A file attached to the draft: uploading as soon as it is picked. */
interface Draft {
  id: string;
  file: File;
  /** An object URL for an image's thumbnail. */
  preview: string | null;
  status: 'uploading' | 'ready' | 'error';
  uploaded?: UploadedFile;
  abort: AbortController;
}

interface Props {
  /**
   * `effort` is the user's pick, or null to leave the conversation's (or the
   * default). `attachments` are uploaded files, named to `startTask`.
   */
  onSubmit: (
    query: string,
    model: string,
    effort: string | null,
    attachments: UploadedFile[],
  ) => void;
  disabled?: boolean;
  /**
   * A run is in flight and sending queues instead of starting a second turn.
   * The composer stays live — this is the whole point of the mode — so
   * `disabled` is false here and every rule that keys off it stays off.
   */
  queueing?: boolean;
  /** Called instead of `onSubmit` while `queueing`. */
  onQueue?: (query: string) => void;
  onStop?: () => void;
  conversationId?: string;
  initialModel?: string;
  /** The conversation's effort pick; null/absent means the default applies. */
  initialEffort?: string | null;
  // Incognito toggle — only wired on the new-chat surface (index page). When
  // provided, an eye-off button lets the user start the conversation ephemeral.
  incognito?: boolean;
  onToggleIncognito?: () => void;
  /**
   * Drop the 760px measure. The dispatch screen already constrains the column
   * around it, so the composer should fill that column rather than sit narrower
   * inside it.
   */
  fullWidth?: boolean;
}

export function InputBox({
  onSubmit,
  disabled = false,
  queueing = false,
  onQueue,
  onStop,
  conversationId,
  initialModel,
  initialEffort,
  incognito = false,
  onToggleIncognito,
  fullWidth = false,
}: Props) {
  const {data: catalog} = useModels();
  const isMobile = useIsMobile();
  const toast = useToast();
  const [model, setModel] = useState('');
  // The user's pick — null until they make one, so a new chat follows the
  // default rather than freezing whatever the default was when it loaded.
  const [effort, setEffort] = useState<string | null>(initialEffort ?? null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  // Drives the send button only: it stays a quiet glyph until there is
  // something to send.
  const [hasText, setHasText] = useState(false);
  const [drafts, setDrafts] = useState<Draft[]>([]);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const draftsRef = useRef<Draft[]>([]);
  draftsRef.current = drafts;

  // Thumbnails are object URLs, held until the draft is sent or dropped.
  useEffect(
    () => () => {
      for (const d of draftsRef.current) {
        d.abort.abort();
        if (d.preview) URL.revokeObjectURL(d.preview);
      }
    },
    [],
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

  useEffect(() => {
    setEffort(initialEffort ?? null);
  }, [conversationId, initialEffort]);

  const modelLevels = catalog?.available.find((m) => m.id === model)?.efforts ?? [];
  const shownEffort = effectiveEffort(effort, catalog?.defaultEffort, modelLevels);

  async function handleEffortChange(level: string) {
    setEffort(level);
    if (conversationId) {
      try {
        await commitUpdateConversation(conversationId, {effort: level});
      } catch (err) {
        console.error('Failed to persist effort change:', err);
      }
    }
  }

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
    setHasText(el.value.trim() !== '');
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 160) + 'px';
    // Scroll only once the text outgrows the cap; below it a classic
    // scrollbar would sit in the field for nothing.
    el.style.overflowY = el.scrollHeight > 160 ? 'auto' : 'hidden';
  }

  function addFiles(files: File[]) {
    if (files.length === 0) return;
    if (queueing) {
      toast.push('Files can’t join a running turn — wait for it to finish, or stop it.', 'error');
      return;
    }
    for (const file of files) {
      if (file.size > MAX_UPLOAD_BYTES) {
        toast.push(
          `${file.name} is ${formatBytes(file.size)} — the limit is ${formatBytes(MAX_UPLOAD_BYTES)}.`,
          'error',
        );
        continue;
      }
      const draft: Draft = {
        id: crypto.randomUUID(),
        file,
        preview: file.type.startsWith('image/') ? URL.createObjectURL(file) : null,
        status: 'uploading',
        abort: new AbortController(),
      };
      setDrafts((prev) => [...prev, draft]);
      uploadFile(file, draft.abort.signal).then(
        (uploaded) =>
          setDrafts((prev) =>
            prev.map((d) => (d.id === draft.id ? {...d, status: 'ready', uploaded} : d)),
          ),
        (err: Error) => {
          if (draft.abort.signal.aborted) return;
          setDrafts((prev) => prev.map((d) => (d.id === draft.id ? {...d, status: 'error'} : d)));
          toast.push(`${file.name} didn’t upload: ${err.message}`, 'error');
        },
      );
    }
  }

  function removeDraft(id: string) {
    setDrafts((prev) => {
      const gone = prev.find((d) => d.id === id);
      if (gone) {
        gone.abort.abort();
        if (gone.preview) URL.revokeObjectURL(gone.preview);
      }
      return prev.filter((d) => d.id !== id);
    });
    textareaRef.current?.focus();
  }

  function handlePaste(e: React.ClipboardEvent<HTMLTextAreaElement>) {
    const files = Array.from(e.clipboardData.files);
    if (files.length === 0) return;
    e.preventDefault();
    addFiles(files);
  }

  function handleDragOver(e: React.DragEvent) {
    if (!e.dataTransfer.types.includes('Files') || disabled) return;
    e.preventDefault();
    setDragging(true);
  }

  function handleDrop(e: React.DragEvent) {
    if (!e.dataTransfer.types.includes('Files') || disabled) return;
    e.preventDefault();
    setDragging(false);
    addFiles(Array.from(e.dataTransfer.files));
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      send();
    }
  }

  function clearInput() {
    const el = textareaRef.current!;
    el.value = '';
    el.style.height = 'auto';
    el.style.overflowY = 'hidden';
    setHasText(false);
  }

  function send() {
    const query = textareaRef.current?.value.trim();
    if (queueing) {
      if (!query || !onQueue) return;
      onQueue(query);
      clearInput();
      return;
    }
    if (uploading || disabled || !model) return;
    const ready = drafts.flatMap((d) => (d.status === 'ready' && d.uploaded ? [d.uploaded] : []));
    if (!query && ready.length === 0) return;
    onSubmit(query ?? '', model, effort, ready);
    clearInput();
    for (const d of drafts) if (d.preview) URL.revokeObjectURL(d.preview);
    setDrafts([]);
  }

  const uploading = drafts.some((d) => d.status === 'uploading');
  const canSend = (hasText || drafts.some((d) => d.status === 'ready')) && !uploading;

  return (
    <div {...stylex.props(composer.wrap, fullWidth && composer.wrapFull)}>
      <div
        {...stylex.props(
          composer.card,
          disabled && composer.cardDisabled,
          queueing && composer.cardQueueing,
          incognito && composer.cardIncognito,
          dragging && composer.cardDragging,
        )}
        onDragOver={handleDragOver}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
      >
        {drafts.length > 0 && (
          <div {...stylex.props(attachment.strip)}>
            {drafts.map((d) => (
              <div
                key={d.id}
                {...stylex.props(
                  attachment.chip,
                  d.preview ? attachment.chipImage : attachment.chipFile,
                  d.status === 'uploading' && attachment.uploading,
                  d.status === 'error' && attachment.failed,
                )}
                title={
                  d.status === 'error'
                    ? `${d.file.name} — upload failed`
                    : `${d.file.name} · ${formatBytes(d.file.size)}`
                }
              >
                {d.preview ? (
                  <img src={d.preview} alt={d.file.name} {...stylex.props(attachment.thumb)} />
                ) : (
                  <>
                    <FileIcon />
                    <span {...stylex.props(attachment.meta)}>
                      <span {...stylex.props(attachment.name)}>{d.file.name}</span>
                      <span {...stylex.props(attachment.size)}>
                        {d.status === 'uploading'
                          ? 'Uploading…'
                          : d.status === 'error'
                            ? 'Upload failed'
                            : formatBytes(d.file.size)}
                      </span>
                    </span>
                  </>
                )}
                <button
                  type="button"
                  {...stylex.props(attachment.remove)}
                  onClick={() => removeDraft(d.id)}
                  title="Remove"
                  aria-label={`Remove ${d.file.name}`}
                >
                  <svg
                    width="10"
                    height="10"
                    viewBox="0 0 24 24"
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="3"
                    strokeLinecap="round"
                  >
                    <line x1="18" y1="6" x2="6" y2="18" />
                    <line x1="6" y1="6" x2="18" y2="18" />
                  </svg>
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
          {...stylex.props(composer.textarea)}
          data-input-textarea=""
          rows={1}
          placeholder={
            queueing
              ? 'Queue a message — delivered at the run’s next step…'
              : incognito
                ? 'Ask anything… (incognito — not saved)'
                : 'Ask anything…'
          }
          disabled={disabled}
          onInput={handleInput}
          onKeyDown={handleKeyDown}
          onPaste={handlePaste}
        />

        <div {...stylex.props(composer.footer)}>
          <button
            type="button"
            {...stylex.props(iconBtn.base, control.glyph)}
            title={queueing ? 'Files can’t join a running turn' : 'Attach files or images'}
            aria-label="Attach files or images"
            disabled={disabled || queueing}
            onClick={() => fileInputRef.current?.click()}
          >
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
              <path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
            </svg>
          </button>
          <input
            ref={fileInputRef}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              addFiles(Array.from(e.target.files ?? []));
              // So the same file can be picked again after removing it.
              e.target.value = '';
            }}
          />

          {onToggleIncognito && (
            <button
              type="button"
              {...stylex.props(iconBtn.base, control.glyph, incognito && control.iconActive)}
              title={
                incognito
                  ? 'Incognito on — this chat won’t be saved'
                  : 'Start an incognito chat (not saved)'
              }
              aria-pressed={incognito}
              disabled={disabled || queueing}
              onClick={onToggleIncognito}
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
                <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19m-6.72-1.07a3 3 0 1 1-4.24-4.24" />
                <line x1="1" y1="1" x2="23" y2="23" />
              </svg>
            </button>
          )}

          <ModelPicker
            models={catalog?.available}
            model={model}
            effort={shownEffort}
            compact={isMobile}
            onModel={(id) => void handleModelChange(id)}
            onEffort={(level) => void handleEffortChange(level)}
          />

          <span {...stylex.props(composer.spacer)} />

          {onStop && (disabled || queueing) ? (
            <>
              <button
                {...stylex.props(control.send, control.sendStop)}
                onClick={onStop}
                title="Stop"
              >
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
              {queueing && (
                <button
                  {...stylex.props(control.send, !hasText && control.sendIdle)}
                  onClick={send}
                  title="Queue message"
                >
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
            </>
          ) : (
            <button
              {...stylex.props(control.send, !canSend && control.sendIdle)}
              onClick={send}
              disabled={disabled || uploading}
              title={uploading ? 'Waiting for the upload to finish' : 'Send'}
            >
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

function FileIcon() {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      {...stylex.props(attachment.icon)}
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}
