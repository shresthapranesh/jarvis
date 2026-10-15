import {commitMutation, graphql} from 'react-relay';

import type {SetEffortsMutation} from '../__generated__/SetEffortsMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation SetEffortsMutation($levels: [String!]!, $default: String) {
    setEfforts(levels: $levels, default: $default) {
      default
      defaultEffort
      effortLevels
      available {
        id
        efforts
      }
    }
  }
`;

/** Which effort levels the composer offers, and the one a chat starts at
 *  (null: by the run's kind). */
export function commitSetEfforts(levels: string[], defaultEffort: string | null) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<SetEffortsMutation>(environment, {
      mutation,
      variables: {levels, default: defaultEffort},
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
