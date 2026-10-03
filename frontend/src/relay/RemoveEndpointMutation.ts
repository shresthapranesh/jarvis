import {commitMutation, graphql} from 'react-relay';

import type {RemoveEndpointMutation} from '../__generated__/RemoveEndpointMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation RemoveEndpointMutation($name: String!) {
    removeEndpoint(name: $name) {
      endpoints {
        name
        baseUrl
        hasKey
      }
    }
  }
`;

export function commitRemoveEndpoint(name: string) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<RemoveEndpointMutation>(environment, {
      mutation,
      variables: {name},
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
