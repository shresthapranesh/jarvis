import type {InfiniteData, QueryClient} from '@tanstack/react-query';

import type {MessagePage, NotificationChannelType} from './types';

// Refetch only the most-recent page of a conversation's infinite query, leaving
// older cached pages intact. Default `invalidateQueries` on an infinite query
// refetches every loaded page, which would re-download the user's whole
// scrolled-up history on every new message — defeating pagination.
export async function refetchConversationFirstPage(
  queryClient: QueryClient,
  conversationId: string,
): Promise<void> {
  const queryKey = ['conversation', conversationId];
  queryClient.setQueryData<InfiniteData<MessagePage, string | undefined>>(
    queryKey,
    (old) => {
      if (!old || old.pages.length === 0) return old;
      return {
        pages: old.pages.slice(0, 1),
        pageParams: old.pageParams.slice(0, 1),
      };
    },
  );
  await queryClient.invalidateQueries({queryKey});
  await queryClient.invalidateQueries({queryKey: ['running-tasks']});
}

export async function checkHealth(): Promise<{status: string}> {
  const res = await fetch('/health');
  if (!res.ok) throw new Error(`Health check failed: ${res.status}`);
  return res.json();
}

export function artifactDownloadUrl(id: string): string {
  return `/artifacts/${id}/raw`;
}

export interface NotificationChannelInput {
  name: string;
  type: NotificationChannelType;
  target: string;
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
