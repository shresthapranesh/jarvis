import {commitMutation, graphql} from 'react-relay';

import type {SetBoardTaskStatusMutation} from '../__generated__/SetBoardTaskStatusMutation.graphql';
import type {BoardTask, BoardTaskStatus} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';
import {mapBoardTask} from './BoardTasksQuery';

const mutation = graphql`
  mutation SetBoardTaskStatusMutation($id: ID!, $status: String!) {
    setBoardTaskStatus(id: $id, status: $status) {
      id
      title
      body
      status
      priority
      createdBy
      model
      skill
      blockedReason
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

export function commitSetBoardTaskStatus(
  rawId: string,
  status: Extract<BoardTaskStatus, 'todo' | 'ready' | 'done' | 'archived'>,
): Promise<BoardTask> {
  return new Promise((resolve, reject) => {
    commitMutation<SetBoardTaskStatusMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('BoardTask', rawId), status},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapBoardTask(response.setBoardTaskStatus));
      },
      onError: reject,
    });
  });
}
