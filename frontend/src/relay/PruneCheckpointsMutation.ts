import {graphql} from 'react-relay';
import {commitMutation} from 'relay-runtime';

import type {
  PruneCheckpointsMutation,
  PruneCheckpointsMutation$data,
  PruneCheckpointsMutation$variables,
} from '../__generated__/PruneCheckpointsMutation.graphql';
import {environment} from './environment';

const mutation = graphql`
  mutation PruneCheckpointsMutation($dryRun: Boolean!) {
    pruneCheckpoints(dryRun: $dryRun) {
      rootPruned
      subgraphPruned
      bytesFreed
      threadsSkippedActive
      dryRun
      note
    }
  }
`;

export function commitPruneCheckpoints(variables: PruneCheckpointsMutation$variables) {
  return new Promise<PruneCheckpointsMutation$data['pruneCheckpoints']>((resolve, reject) => {
    commitMutation<PruneCheckpointsMutation>(environment, {
      mutation,
      variables,
      onCompleted: (res, errors) =>
        errors?.length ? reject(errors[0]) : resolve(res.pruneCheckpoints),
      onError: reject,
    });
  });
}
