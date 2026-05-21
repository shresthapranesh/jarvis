import {commitMutation, graphql} from 'react-relay';

import type {CreateNotificationChannelMutation} from '../__generated__/CreateNotificationChannelMutation.graphql';
import type {NotificationChannel, NotificationChannelType} from '../lib/types';
import {environment} from './environment';
import {mapChannel} from './NotificationChannelsQuery';

const mutation = graphql`
  mutation CreateNotificationChannelMutation($input: NotificationChannelCreateInput!) {
    createNotificationChannel(input: $input) {
      id
      name
      type
      target
      createdAt
      updatedAt
    }
  }
`;

interface CreatePayload {
  name: string;
  type: NotificationChannelType;
  target: string;
}

export function commitCreateNotificationChannel(p: CreatePayload): Promise<NotificationChannel> {
  return new Promise((resolve, reject) => {
    commitMutation<CreateNotificationChannelMutation>(environment, {
      mutation,
      variables: {input: p},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapChannel(response.createNotificationChannel));
      },
      onError: reject,
    });
  });
}
