import {commitMutation, graphql} from 'react-relay';

import type {DeleteProjectMutation} from '../__generated__/DeleteProjectMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteProjectMutation($id: ID!) {
    deleteProject(id: $id)
  }
`;

export function commitDeleteProject(rawId: string): Promise<void> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteProjectMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Project', rawId)},
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
