import {commitMutation, graphql} from 'react-relay';

import type {DeleteArtifactMutation} from '../__generated__/DeleteArtifactMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteArtifactMutation($id: ID!) {
    deleteArtifact(id: $id)
  }
`;

export function commitDeleteArtifact(rawId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteArtifactMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Artifact', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.deleteArtifact);
      },
      onError: reject,
    });
  });
}
