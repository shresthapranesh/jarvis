import {commitMutation, graphql} from 'react-relay';

import type {StopBoardTaskMutation} from '../__generated__/StopBoardTaskMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation StopBoardTaskMutation($id: ID!) {
    stopBoardTask(id: $id)
  }
`;

export function commitStopBoardTask(rawId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    commitMutation<StopBoardTaskMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('BoardTask', rawId)},
      onCompleted: (_response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve();
      },
      onError: reject,
    });
  });
}
