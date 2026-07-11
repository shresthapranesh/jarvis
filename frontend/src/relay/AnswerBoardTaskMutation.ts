import {commitMutation, graphql} from 'react-relay';

import type {AnswerBoardTaskMutation} from '../__generated__/AnswerBoardTaskMutation.graphql';
import type {BoardTask} from '../lib/types';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';
import {mapBoardTask} from './BoardTasksQuery';

const mutation = graphql`
  mutation AnswerBoardTaskMutation($id: ID!, $answer: String!) {
    answerBoardTask(id: $id, answer: $answer) {
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

export function commitAnswerBoardTask(rawId: string, answer: string): Promise<BoardTask> {
  return new Promise((resolve, reject) => {
    commitMutation<AnswerBoardTaskMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('BoardTask', rawId), answer},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(mapBoardTask(response.answerBoardTask));
      },
      onError: reject,
    });
  });
}
