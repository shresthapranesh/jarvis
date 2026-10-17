import {commitMutation, graphql} from 'react-relay';

import type {SetModelEffortsMutation} from '../__generated__/SetModelEffortsMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation SetModelEffortsMutation($id: String!, $levels: [String!]) {
    setModelEfforts(id: $id, levels: $levels) {
      default
      defaultEffort
      available {
        id
        efforts
        effortsCustomized
      }
    }
  }
`;

/** The effort levels a model takes; null goes back to its defaults. */
export function commitSetModelEfforts(id: string, levels: string[] | null) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<SetModelEffortsMutation>(environment, {
      mutation,
      variables: {id, levels},
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
