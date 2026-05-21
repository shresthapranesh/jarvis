import {commitMutation, graphql} from 'react-relay';

import type {DeleteDocumentMutation} from '../__generated__/DeleteDocumentMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation DeleteDocumentMutation($id: ID!) {
    deleteDocument(id: $id)
  }
`;

export function commitDeleteDocument(rawId: string): Promise<boolean> {
  return new Promise((resolve, reject) => {
    commitMutation<DeleteDocumentMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Document', rawId)},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.deleteDocument);
      },
      onError: reject,
    });
  });
}
