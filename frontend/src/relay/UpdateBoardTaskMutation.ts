import {commitMutation, graphql} from 'react-relay';

import type {UpdateBoardTaskMutation} from '../__generated__/UpdateBoardTaskMutation.graphql';
import type {BoardTask} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';
import {mapBoardTask} from './BoardTasksQuery';

const mutation = graphql`
  mutation UpdateBoardTaskMutation($id: ID!, $input: BoardTaskUpdateInput!) {
    updateBoardTask(id: $id, input: $input) {
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

export interface UpdateBoardTaskPayload {
  title?: string;
  body?: string;
  priority?: number;
  model?: string | null;
  skill?: string | null;
}

export function commitUpdateBoardTask(rawId: string, p: UpdateBoardTaskPayload): Promise<BoardTask> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateBoardTaskMutation>(environment, {
      mutation,
      variables: {
        id: encodeGlobalId('BoardTask', rawId),
        input: {
          title: p.title ?? null,
          body: p.body ?? null,
          priority: p.priority ?? null,
          model: p.model ?? null,
          skill: p.skill ?? null,
        },
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapBoardTask(response.updateBoardTask));
      },
      onError: reject,
    });
  });
}
