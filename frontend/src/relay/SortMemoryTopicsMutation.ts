import {commitMutation, graphql} from 'react-relay';

import type {SortMemoryTopicsMutation} from '../__generated__/SortMemoryTopicsMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation SortMemoryTopicsMutation($model: String) {
    sortMemoryTopics(model: $model)
  }
`;

/** Give each unsorted memory a topic (and tidy its wording) now, instead of on the timer. */
export function commitSortMemoryTopics(model?: string): Promise<string> {
  return new Promise((resolve, reject) => {
    commitMutation<SortMemoryTopicsMutation>(environment, {
      mutation,
      variables: {model: model ?? null},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve(response.sortMemoryTopics);
      },
      onError: reject,
    });
  });
}
