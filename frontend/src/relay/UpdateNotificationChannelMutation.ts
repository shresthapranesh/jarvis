import {commitMutation, graphql} from 'react-relay';

import type {UpdateNotificationChannelMutation} from '../__generated__/UpdateNotificationChannelMutation.graphql';
import type {NotificationChannel, NotificationChannelType} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';
import {mapChannel} from './NotificationChannelsQuery';

const mutation = graphql`
  mutation UpdateNotificationChannelMutation($id: ID!, $input: NotificationChannelUpdateInput!) {
    updateNotificationChannel(id: $id, input: $input) {
      id
      name
      type
      target
      createdAt
      updatedAt
    }
  }
`;

interface UpdatePayload {
  name?: string;
  type?: NotificationChannelType;
  target?: string;
}

export function commitUpdateNotificationChannel(
  rawId: string,
  p: UpdatePayload,
): Promise<NotificationChannel> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateNotificationChannelMutation>(environment, {
      mutation,
      variables: {
        id: encodeGlobalId('NotificationChannel', rawId),
        input: {
          name: p.name ?? null,
          type: p.type ?? null,
          target: p.target ?? null,
        },
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapChannel(response.updateNotificationChannel));
      },
      onError: reject,
    });
  });
}
