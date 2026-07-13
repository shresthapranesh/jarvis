import {commitMutation, graphql} from 'react-relay';

import type {UpdateProjectMutation} from '../__generated__/UpdateProjectMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation UpdateProjectMutation($id: ID!, $input: ProjectUpdateInput!) {
    updateProject(id: $id, input: $input) {
      id
      name
      description
      instructions
      memory
      updatedAt
    }
  }
`;

export interface UpdateProjectPatch {
  name?: string | null;
  description?: string | null;
  instructions?: string | null;
  memory?: string | null;
}

export function commitUpdateProject(rawId: string, patch: UpdateProjectPatch): Promise<void> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateProjectMutation>(environment, {
      mutation,
      variables: {
        id: encodeGlobalId('Project', rawId),
        input: {
          name: patch.name ?? null,
          description: patch.description ?? null,
          instructions: patch.instructions ?? null,
          memory: patch.memory ?? null,
        },
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
