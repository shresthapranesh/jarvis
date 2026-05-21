import type {MediaAttachment} from './types';

interface UploadResult {
  uploadId: string;
  filename: string;
  mimeType: string;
  size: number;
}

export async function uploadStagedAttachment(att: MediaAttachment): Promise<UploadResult> {
  const base64 = att.dataUrl.split(',')[1] ?? att.dataUrl;
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  const blob = new Blob([bytes], {type: att.mimeType});
  const form = new FormData();
  form.append('file', blob, att.name);
  const res = await fetch('/uploads', {method: 'POST', body: form});
  if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
  return res.json();
}
