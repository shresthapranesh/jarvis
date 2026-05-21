import {commitMutation, graphql} from 'react-relay';

import type {RunWorkflowMutation} from '../__generated__/RunWorkflowMutation.graphql';
import {environment} from './environment';
import {encodeGlobalId} from './globalId';

const mutation = graphql`
  mutation RunWorkflowMutation($id: ID!, $inputs: JSON) {
    runWorkflow(id: $id, inputs: $inputs)
  }
`;

export function commitRunWorkflow(
  rawId: string,
  inputs: Record<string, unknown> | null,
): Promise<{run_id: string}> {
  return new Promise((resolve, reject) => {
    commitMutation<RunWorkflowMutation>(environment, {
      mutation,
      variables: {id: encodeGlobalId('Workflow', rawId), inputs},
      onCompleted: (response, errors) => {
        if (errors && errors.length > 0) {
          reject(new Error(errors[0].message));
          return;
        }
        resolve({run_id: response.runWorkflow});
      },
      onError: reject,
    });
  });
}
