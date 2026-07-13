import {commitMutation, graphql} from 'react-relay';

import type {CreateProjectMutation} from '../__generated__/CreateProjectMutation.graphql';
import {environment} from './environment';
import {decodeGlobalId} from './globalId';

const mutation = graphql`
  mutation CreateProjectMutation($input: ProjectCreateInput!) {
    createProject(input: $input) {
      id
      name
    }
  }
`;

export interface CreateProjectPayload {
  name: string;
  description?: string | null;
  instructions?: string;
}

export function commitCreateProject(p: CreateProjectPayload): Promise<{id: string; name: string}> {
  return new Promise((resolve, reject) => {
    commitMutation<CreateProjectMutation>(environment, {
      mutation,
      variables: {
        input: {
          name: p.name,
          description: p.description ?? null,
          instructions: p.instructions ?? '',
        },
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve({
          id: decodeGlobalId(response.createProject.id),
          name: response.createProject.name,
        });
      },
      onError: reject,
    });
  });
}
