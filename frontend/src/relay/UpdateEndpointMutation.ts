import {commitMutation, graphql} from 'react-relay';

import type {UpdateEndpointMutation} from '../__generated__/UpdateEndpointMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation UpdateEndpointMutation(
    $name: String!
    $baseUrl: String!
    $apiKey: String
    $clearKey: Boolean!
  ) {
    updateEndpoint(name: $name, baseUrl: $baseUrl, apiKey: $apiKey, clearKey: $clearKey) {
      endpoints {
        name
        baseUrl
        hasKey
      }
    }
  }
`;

/** `apiKey` null keeps the stored key — it never comes back to the browser,
 *  so the form can't resend it. `clearKey` drops it. */
export function commitUpdateEndpoint(
  name: string,
  baseUrl: string,
  apiKey: string | null,
  clearKey: boolean,
) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<UpdateEndpointMutation>(environment, {
      mutation,
      variables: {name, baseUrl, apiKey, clearKey},
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
