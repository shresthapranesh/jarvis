import {commitMutation, graphql} from 'react-relay';

import type {SetConversationProjectMutation} from '../__generated__/SetConversationProjectMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation SetConversationProjectMutation($conversationId: ID!, $projectId: ID) {
    setConversationProject(conversationId: $conversationId, projectId: $projectId) {
      id
      projectId
    }
  }
`;

/** Assign a conversation to a project; pass projectRawId=null to remove it. */
export function commitSetConversationProject(
  convRawId: string,
  projectRawId: string | null,
): Promise<void> {
  return new Promise((resolve, reject) => {
    commitMutation<SetConversationProjectMutation>(environment, {
      mutation,
      variables: {
        conversationId: encodeGlobalId('Conversation', convRawId),
        projectId: projectRawId ? encodeGlobalId('Project', projectRawId) : null,
      },
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
