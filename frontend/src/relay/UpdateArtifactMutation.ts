import {commitMutation, graphql} from 'react-relay';

import type {UpdateArtifactMutation, UpdateArtifactMutation$data} from '../__generated__/UpdateArtifactMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation UpdateArtifactMutation($id: ID!, $title: String, $content: String) {
    updateArtifact(id: $id, title: $title, content: $content) {
      id
      title
      content
      updatedAt
    }
  }
`;

export function commitUpdateArtifact(
  rawId: string,
  patch: {title?: string; content?: string},
): Promise<UpdateArtifactMutation$data['updateArtifact']> {
  return new Promise((resolve, reject) => {
    commitMutation<UpdateArtifactMutation>(environment, {
      mutation,
      variables: {
        id: encodeGlobalId('Artifact', rawId),
        title: patch.title ?? null,
        content: patch.content ?? null,
      },
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.updateArtifact);
      },
      onError: reject,
    });
  });
}
