import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {ArtifactListQuery} from '../__generated__/ArtifactListQuery.graphql';
import {environment} from './environment';

export const artifactListQuery = graphql`
  query ArtifactListQuery($conversationId: String) {
    artifacts(conversationId: $conversationId) {
      id
      title
      kind
      mimeType
      conversationId
      messageId
      createdAt
      updatedAt
    }
  }
`;

export function refreshArtifactList(conversationId: string | null) {
  return fetchQuery<ArtifactListQuery>(
    environment,
    artifactListQuery,
    {conversationId},
    {fetchPolicy: 'network-only'},
  )
    .toPromise()
    .catch(() => undefined);
}
