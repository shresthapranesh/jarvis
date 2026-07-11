import {commitMutation, graphql} from 'react-relay';

import type {DeleteBoardTaskMutation} from '../__generated__/DeleteBoardTaskMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteBoardTaskMutation($id: ID!) {
    deleteBoardTask(id: $id)
  }
`;

export function commitDeleteBoardTask(rawId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteBoardTaskMutation>(environment, {
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
