import {commitMutation, graphql} from 'react-relay';

import type {DecomposeBoardTaskMutation} from '../__generated__/DecomposeBoardTaskMutation.graphql';
import type {BoardTask} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';
import {mapBoardTask} from './BoardTasksQuery';

const mutation = graphql`
  mutation DecomposeBoardTaskMutation($id: ID!) {
    decomposeBoardTask(id: $id) {
      id
      title
      body
      status
      priority
      createdBy
      model
      skill
      blockedReason
      blockedKind
      failureCount
      summary
      resultMetadata
      conversationId
      runId
      parentIds
      childIds
      createdAt
      updatedAt
      startedAt
      finishedAt
    }
  }
`;

export function commitDecomposeBoardTask(rawId: string): Promise<BoardTask[]> {
  return new Promise((resolve, reject) => {
    commitMutation<DecomposeBoardTaskMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('BoardTask', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.decomposeBoardTask.map(mapBoardTask));
      },
      onError: reject,
    });
  });
}
