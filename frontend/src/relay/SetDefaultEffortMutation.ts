import {commitMutation, graphql} from 'react-relay';

import type {SetDefaultEffortMutation} from '../__generated__/SetDefaultEffortMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation SetDefaultEffortMutation($level: String) {
    setDefaultEffort(level: $level) {
      default
      defaultEffort
    }
  }
`;

/** The effort a chat starts at; null leaves it to the run's kind. */
export function commitSetDefaultEffort(level: string | null) {
  return new Promise<void>((resolve, reject) => {
    commitMutation<SetDefaultEffortMutation>(environment, {
      mutation,
      variables: {level},
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
