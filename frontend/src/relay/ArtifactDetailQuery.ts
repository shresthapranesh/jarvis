import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ArtifactDetailQuery} from '../__generated__/ArtifactDetailQuery.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

export const artifactDetailQuery = graphql`
  query ArtifactDetailQuery($id: ID!) {
    artifact(id: $id) {
      id
      title
      content
      kind
      mimeType
      filename
      conversationId
      messageId
      createdAt
      updatedAt
      versionCount
      versions {
        id
        artifactId
        version
        title
        filename
        createdAt
        content
      }
    }
  }
`;

export function refreshArtifactDetail(rawId: string) {
  return fetchQuery<ArtifactDetailQuery>(
    environment,
    artifactDetailQuery,
    {id: encodeGlobalId('Artifact', rawId)},
    {fetchPolicy: 'network-only'},
  )
    .toPromise()
    .catch(() => undefined);
}
