import {commitMutation, graphql} from 'react-relay';

import type {AddEndpointMutation} from '../__generated__/AddEndpointMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation AddEndpointMutation($name: String!, $baseUrl: String!, $apiKey: String) {
    addEndpoint(name: $name, baseUrl: $baseUrl, apiKey: $apiKey) {
      endpoints {
        name
        baseUrl
        hasKey
      }
    }
  }
`;

export function commitAddEndpoint(name: string, baseUrl: string, apiKey: string | null) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<AddEndpointMutation>(environment, {
      mutation,
      variables: {name, baseUrl, apiKey},
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
