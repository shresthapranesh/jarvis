import {graphql} from 'react-relay';
import {fetchQuery} from 'relay-runtime';

import type {DocumentListQuery} from '../__generated__/DocumentListQuery.graphql';
import {environment} from './environment';

export const documentListQuery = graphql`
  query DocumentListQuery($conversationId: String!) {
    documents(conversationId: $conversationId) {
      id
      conversationId
      messageId
      filename
      mimeType
      size
      createdAt
    }
  }
`;

export function refreshDocumentList(conversationId: string) {
  return fetchQuery<DocumentListQuery>(
    environment,
    documentListQuery,
    {conversationId},
    {fetchPolicy: 'network-only'},
  )
    .toPromise()
    .catch(() => undefined);
}
