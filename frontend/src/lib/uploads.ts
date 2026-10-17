/**
 * Chat attachments: a file is sent to `POST /uploads` the moment it is
 * attached, and `startTask` names it by the id that comes back
 * (`attachmentUploads`). The server keeps it where the agent can open it, and
 * shows a small enough image to the model too (`edge/src/uploads.rs`).
 */

/** Mirrors `MAX_UPLOAD_BYTES` in `edge/src/uploads.rs`; refused here first. */
export const MAX_UPLOAD_BYTES = 100 * 1024 * 1024;

export interface UploadedFile {
  uploadId: string;
  filename: string;
  mimeType: string;
  size: number;
}

export async function uploadFile(file: File, signal?: AbortSignal): Promise<UploadedFile> {
  const form = new FormData();
  form.append('file', file, file.name);
  const res = await fetch('/uploads', {method: 'POST', body: form, signal});
  if (!res.ok) {
    const body = (await res.json().catch(() => null)) as {error?: string} | null;
    throw new Error(body?.error ?? `upload failed (${res.status})`);
  }
  return res.json();
}

export function formatBytes(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 ** 2).toFixed(n >= 10 * 1024 ** 2 ? 0 : 1)} MB`;
  if (n >= 1024) return `${Math.round(n / 1024)} KB`;
  return `${n} B`;
}

/**
 * The user message as the server stores one with attachments — its text, then
 * a chip per file — so an optimistic bubble renders the same as the real one.
 */
export function messageContent(query: string, files: UploadedFile[]): string {
  if (files.length === 0) return query;
  const kind = (mime: string) =>
    ['image', 'audio', 'video'].find((k) => mime.startsWith(`${k}/`)) ?? 'document';
  return JSON.stringify([
    {type: 'text', text: query},
    ...files.map((f) => ({
      type: kind(f.mimeType),
      name: f.filename,
      size: f.size,
      mimeType: f.mimeType,
    })),
  ]);
}
