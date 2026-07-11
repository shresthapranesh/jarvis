import {commitMutation, graphql} from 'react-relay';

import type {CreateBoardTaskMutation} from '../__generated__/CreateBoardTaskMutation.graphql';
import type {BoardTask, CreateBoardTaskPayload} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';
import {mapBoardTask} from './BoardTasksQuery';

const mutation = graphql`
  mutation CreateBoardTaskMutation($input: BoardTaskInput!) {
    createBoardTask(input: $input) {
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

export function commitCreateBoardTask(p: CreateBoardTaskPayload): Promise<BoardTask> {
  return new Promise((resolve, reject) => {
    commitMutation<CreateBoardTaskMutation>(environment, {
      mutation,
      variables: {
        input: {
          title: p.title,
          body: p.body ?? null,
          priority: p.priority ?? 0,
          model: p.model ?? null,
          skill: p.skill ?? null,
          parentIds: p.parentIds?.length
            ? p.parentIds.map((id) => encodeGlobalId('BoardTask', id))
            : null,
          start: p.start ?? true,
        },
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapBoardTask(response.createBoardTask));
      },
      onError: reject,
    });
  });
}
