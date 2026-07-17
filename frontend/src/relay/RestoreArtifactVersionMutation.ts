import {graphql} from 'react-relay';
import {commitMutation} from 'react-relay';
import type {RestoreArtifactVersionMutation} from '../__generated__/RestoreArtifactVersionMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId, decodeGlobalId} from './globalId';

const mutation = graphql`
  mutation RestoreArtifactVersionMutation($id: ID!, $version: Int!) {
    restoreArtifactVersion(id: $id, version: $version) {
      id
      title
      content
      updatedAt
      versionCount
    }
  }
`;

export function commitRestoreArtifactVersion(rawId: string, version: number): Promise<void> {
  return new Promise((resolve, reject) => {
    commitMutation<RestoreArtifactVersionMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Artifact', rawId), version},
      onCompleted: (_res, errors) => {
        if (errors && errors.length) {
          reject(new Error(errors.map((e) => e.message).join('; ')));
          return;
        }
        resolve();
      },
      onError: reject,
    });
  });
}
