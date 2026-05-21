import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {
  NotificationChannelsQuery,
  NotificationChannelsQuery$data,
} from '../__generated__/NotificationChannelsQuery.graphql';
import type {NotificationChannel, NotificationChannelType} from '../lib/types';
import {environment} from './environment';
import {decodeGlobalId} from './globalId';

export const notificationChannelsQuery = graphql`
  query NotificationChannelsQuery {
    notificationChannels {
      id
      name
      type
      target
      createdAt
      updatedAt
    }
  }
`;

type Node = NotificationChannelsQuery$data['notificationChannels'][number];

export function mapChannel(c: Node): NotificationChannel {
  return {
    id: decodeGlobalId(c.id),
    name: c.name,
    type: c.type as NotificationChannelType,
    target: c.target,
    created_at: c.createdAt,
    updated_at: c.updatedAt,
  };
}

export async function fetchNotificationChannels(): Promise<NotificationChannel[]> {
  const data = await fetchQuery<NotificationChannelsQuery>(
    environment,
    notificationChannelsQuery,
    {},
    {fetchPolicy: 'network-only'},
  ).toPromise();
  return (data?.notificationChannels ?? []).map(mapChannel);
}

export function refreshNotificationChannels() {
  return fetchNotificationChannels().catch(() => undefined);
}
